from contextlib import closing, contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from tempfile import TemporaryDirectory
import unittest

from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import source_sessions as reads
from robert_agent.persistence.rounds import pool, source_model
from robert_agent.persistence.schema import native_sources
from robert_agent.persistence.schema.database import init_database
from robert_agent.persistence.stores import ingress_requests, source_sessions as sessions

NOW = "2026-11-14T14:30:00+00:00"
LATER = "2026-11-14T14:30:11+00:00"
IDENTITY = {
    "pid": 1234,
    "process_group_id": 1234,
    "identity_source": "proc",
    "process_start_token": "mock-start",
    "command_fingerprint": "e" * 64,
}


class SourceSessionTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.db = Path(scratch.name) / "db.sqlite"
        init_database(self.db)
        self.sql("""
            INSERT INTO sources(source_id,source_key,provider,host,project_key,source_type,external_id)
              VALUES('s','tapd:s','tapd','tapd.example','p','story','001'),
                    ('other','tapd:other','tapd','tapd.example','p','bug','002');
            INSERT INTO source_events(event_id,source_id,event_fingerprint,event_type,event_at,payload_json)
              VALUES('e','s','first','comment','1','{}'),('later','s','second','comment','2','{}'),
                    ('other','other','foreign-first','comment','1','{}');
            INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root)
              VALUES('r','org/repo','bot','main','/repo','/trees'),('foreign','org/else','bot','main','/else','/more');
            INSERT INTO workstreams(workstream_id,repo_id,created_at,updated_at)
              VALUES('w','r','now','now'),('another','r','now','now'),('f','foreign','now','now');
            INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at)
              VALUES('task','w','queued','now','now'),('next','another','queued','now','now'),
                    ('foreign','f','queued','now','now');
        """)
        with self.writer() as conn:
            for request_id, source_id, event in (
                ("request", "s", "e"),
                ("more", "s", "later"),
                ("foreign", "other", "other"),
            ):
                ingress_requests.enqueue(
                    conn,
                    request_id=request_id,
                    source_id=source_id,
                    provider="tapd",
                    auth_type="mention",
                    authorization={"mode": "readonly"},
                    content_snapshot={"text": "frozen"},
                    content_version="v1",
                    event_ids=(event,),
                    now=NOW,
                )
            self.claim = sessions.claim(conn, source_id="s", owner="a", now=NOW, lease_seconds=10)
            self.start(conn)

    def sql(self, script):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(script)

    @contextmanager
    def writer(self):
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            yield conn

    def start(self, conn, *, claim=None, round_id="round", members=None):
        return sessions.start_round(
            conn,
            claim or self.claim,
            round_id=round_id,
            member_versions=members or {"request": "v1"},
            input_version="v1",
            input_snapshot={"context": "bounded"},
            capability="readonly",
            repo_ids=("r",),
            publication_scope={"tapd": "comment"},
            now=NOW,
        )

    def bind(self, conn, task_id="task", *, claim=None, now=NOW):
        return sessions.bind_task(
            conn,
            claim or self.claim,
            round_id="round",
            request_id="request",
            task_id=task_id,
            capability="readonly",
            now=now,
        )

    def finish(self, conn, *, claim=None, now=NOW):
        return sessions.finish_round(conn, claim or self.claim, round_id="round", outcome="execution_finished", now=now)

    def packet(self, run_id):
        body = "frozen source prompt"
        digest = sha256(body.encode()).hexdigest()
        return dict(
            prompt_sha256=digest,
            input_packet={
                "version": 1,
                "payload": json.dumps(
                    {
                        "round_id": "round",
                        "run_id": run_id,
                        "input_version": "v1",
                        "prompt_sha256": digest,
                        "prompt": body,
                    }
                ),
            },
        )

    def reserve_and_grant(self, conn, run_id):
        source_model.enqueue(
            conn,
            self.claim,
            run_id=run_id,
            round_id="round",
            request_id="request",
            stage="identify",
            worker_id="worker",
            input_version="v1",
            **self.packet(run_id),
            now=NOW,
        )
        self.assertTrue(source_model.reserve_slot(conn, self.claim, run_id, now=NOW))
        self.assertTrue(source_model.authorize_launch(conn, self.claim, run_id, launch_id="launch", now=NOW))

    def test_legacy_run_without_input_packet_cannot_receive_launch_grant(self):
        with self.writer() as conn:
            source_model.enqueue(
                conn,
                self.claim,
                run_id="legacy",
                round_id="round",
                request_id="request",
                stage="identify",
                worker_id="worker",
                input_version="v1",
                prompt_sha256="b" * 64,
                now=NOW,
            )
            self.assertFalse(source_model.reserve_slot(conn, self.claim, "legacy", now=NOW))
            self.assertFalse(source_model.authorize_launch(conn, self.claim, "legacy", launch_id="no", now=NOW))
            self.assertTrue(source_model.finish(conn, self.claim, "legacy", outcome="interrupted", now=NOW))

    def test_two_connections_fence_stale_owner_without_releasing_active_round(self):
        with self.writer() as conn:
            self.assertIsNone(sessions.claim(conn, source_id="s", owner="b", now=NOW, lease_seconds=10))
            self.assertTrue(self.bind(conn))
        self.sql("""
            UPDATE tasks SET lifecycle='completed' WHERE task_id='task';
            INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json)
              VALUES('attempt','task',1,'canceled','{"dispatch":{"pid":123,"process_exited":true}}');
        """)
        with self.writer() as conn:
            replacement = sessions.claim(conn, source_id="s", owner="b", now=LATER, lease_seconds=20)
            self.assertGreater(replacement.token, self.claim.token)
            self.assertEqual(reads.active_round(conn, "s")["round_id"], "round")
            self.assertFalse(self.finish(conn, now=LATER))
            self.assertFalse(self.finish(conn, claim=replacement, now=LATER))
            self.assertEqual(reads.blockers(conn, "round")[0].identity, "attempt")
            self.assertFalse(self.bind(conn, "next", claim=replacement, now=LATER))
        self.sql("""UPDATE attempts SET metadata_json='{"process_stopped_at":"2026-11-14T14:30:08+00:00",
                    "dispatch":{"pid":123}}' WHERE attempt_id='attempt';""")
        with self.writer() as conn:
            self.assertTrue(self.bind(conn, "next", claim=replacement, now=LATER))
            self.assertEqual([item["task_id"] for item in reads.round_tasks(conn, "round")], ["task", "next"])

    def test_scope_membership_and_frozen_evidence(self):
        with self.writer() as conn:
            self.assertFalse(self.bind(conn, "foreign"))
            self.assertIsNone(self.start(conn, round_id="replacement", members={"more": "v1"}))
            self.assertEqual(reads.round_requests(conn, "round")[0]["request_id"], "request")
            other = sessions.claim(conn, source_id="other", owner="other", now=NOW, lease_seconds=10)
            self.assertIsNone(self.start(conn, claim=other, round_id="foreign", members={"more": "v1"}))
            self.assertFalse(
                sessions.bind_task(
                    conn,
                    self.claim,
                    round_id="round",
                    request_id="request",
                    task_id="task",
                    capability="development",
                    now=NOW,
                )
            )
        self.sql("""UPDATE ingress_requests SET content_version='v2',content_snapshot_json='{"text":"edited"}'
                    WHERE request_id='request';""")
        with self.writer() as conn:
            record = reads.round_requests(conn, "round")[0]
            self.assertEqual(record["content_version"], "v1")
            self.assertIn("frozen", record["content_snapshot_json"])
            self.assertTrue(self.finish(conn))
            self.assertIsNotNone(self.start(conn, round_id="second", members={"more": "v1"}))

    def test_task_effects_and_native_model_slots_block_release(self):
        with self.writer() as conn:
            self.assertTrue(self.bind(conn))
        self.sql("""
            UPDATE tasks SET lifecycle='waiting_for_user' WHERE task_id='task';
            INSERT INTO attempts(attempt_id,task_id,attempt_no,status,started_at)
              VALUES('never-launched','task',1,'prepared','2026-11-14T14:30:00+00:00');
            INSERT INTO review_sessions(round_id,repo,pr_number,workstream_id,trigger_fingerprint,task_id,request_json,created_at,updated_at)
              VALUES('review','org/repo',1,'w','trigger','task','{}','now','now');
            INSERT INTO review_runs(run_id,round_id,stage,worker_id,attempt_no,owner_attempt_id,status,
              output_dir,prompt_sha256,slot_reserved,created_at,updated_at)
              VALUES('review-run','review','review','reviewer',1,'never-launched','failed','dir','hash',1,'now','now');
            INSERT INTO github_actions(action_id,task_id,action_type,audit_status,created_at)
              VALUES('action','task','comment','accepted','now');
            INSERT INTO publication_intents(publication_id,action_id,idempotency_key,request_fingerprint,status,created_at,updated_at)
              VALUES('publish','action','key','fp','attention','now','now');
        """)
        with self.writer() as conn:
            self.assertEqual({item.identity for item in reads.blockers(conn, "round")}, {"review-run", "publish"})
            self.assertFalse(
                sessions.finish_round(conn, self.claim, round_id="round", outcome="waiting_for_user", now=NOW)
            )
        self.sql("""
            UPDATE review_runs SET slot_reserved=0;
            UPDATE publication_intents SET status='published';
            UPDATE github_actions SET publish_status='published';
        """)
        with self.writer() as conn:
            self.assertTrue(
                sessions.finish_round(conn, self.claim, round_id="round", outcome="waiting_for_user", now=NOW)
            )

    def test_shared_pool_cannot_release_unknown_exit_or_accept_old_owner(self):
        self.sql("UPDATE review_process_pool SET max_concurrency=1;")
        with self.writer() as conn:
            self.reserve_and_grant(conn, "identify")
            self.assertFalse(pool.reserve(conn, "review_runs", "missing", NOW))
            self.assertEqual(pool.status(conn), {"limit": 1, "in_use": 1})
        with self.writer() as conn:
            current = sessions.claim(conn, source_id="s", owner="b", now=LATER, lease_seconds=20)
            self.assertFalse(self.finish(conn, claim=current, now=LATER))
            self.assertFalse(
                source_model.finish(
                    conn, self.claim, "identify", launch_id="launch", outcome="failed", error="obsolete", now=LATER
                )
            )
            self.assertFalse(
                source_model.finish(
                    conn, current, "identify", launch_id="launch", outcome="failed", error="unknown", now=LATER
                )
            )
            self.assertFalse(
                source_model.finish(
                    conn,
                    current,
                    "identify",
                    launch_id="launch",
                    outcome="failed",
                    exit_kind="spawn_failed",
                    now=LATER,
                )
            )
            self.assertEqual(pool.status(conn)["in_use"], 1)
            self.assertFalse(
                source_model.record_identity(
                    conn, self.claim, "identify", launch_id="launch", identity=IDENTITY, now=LATER
                )
            )
            self.assertTrue(
                source_model.record_identity(
                    conn, current, "identify", launch_id="launch", identity=IDENTITY, now=LATER
                )
            )
            self.assertFalse(
                source_model.finish(
                    conn,
                    current,
                    "identify",
                    launch_id="launch",
                    outcome="succeeded",
                    output={"repo": "r"},
                    exit_kind="group_exited",
                    identity={**IDENTITY, "process_start_token": "different"},
                    now=LATER,
                )
            )
            self.assertTrue(
                source_model.finish(
                    conn,
                    current,
                    "identify",
                    launch_id="launch",
                    outcome="succeeded",
                    output={"repo": "r"},
                    exit_kind="group_exited",
                    identity=IDENTITY,
                    now=LATER,
                )
            )
            self.assertEqual(pool.status(conn)["in_use"], 0)
            self.assertTrue(self.finish(conn, claim=current, now=LATER))

    def test_corrupt_metadata_remains_busy_and_other_source_independent(self):
        with self.writer() as conn:
            self.assertTrue(self.bind(conn))
        self.sql("""
            UPDATE tasks SET lifecycle='failed' WHERE task_id='task';
            INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json)
              VALUES('corrupt','task',1,'failed','[');
        """)
        with self.writer() as conn:
            self.assertFalse(self.finish(conn))
            self.assertEqual(reads.blockers(conn, "round")[0].reason, "attempt_evidence_invalid")
            other = sessions.claim(conn, source_id="other", owner="b", now=NOW, lease_seconds=30)
            self.assertIsNotNone(self.start(conn, claim=other, round_id="different", members={"foreign": "v1"}))

    def test_competing_claimants_only_one_can_open_expired_session(self):
        barrier = Barrier(2)

        def worker(owner):
            barrier.wait(timeout=5)
            with self.writer() as conn:
                return sessions.claim(conn, source_id="s", owner=owner, now=LATER, lease_seconds=10)

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(worker, ("b", "c")))
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        with self.writer() as conn:
            self.assertEqual(reads.session(conn, "s")["token"], self.claim.token + 1)
            self.assertEqual(reads.active_round(conn, "s")["round_id"], "round")

    def test_repository_decision_blocks_readonly_binding_and_revoked_child_launch(self):
        self.sql("""
            UPDATE tasks SET route_id='pinned-custom-route' WHERE task_id='task';
            INSERT INTO route_decisions(route_decision_id,task_id,route_id,expected_output,
              allowed_github_actions_json,confidence,created_at)
              VALUES('route','task','pinned-custom-route','new_pr','["open_pr"]','high','now');
        """)
        with self.writer() as conn:
            self.assertFalse(self.bind(conn))
            source_model.enqueue(
                conn,
                self.claim,
                run_id="unlaunched",
                round_id="round",
                request_id="request",
                stage="identify",
                worker_id="worker",
                input_version="v1",
                **self.packet("unlaunched"),
                now=NOW,
            )
            self.assertTrue(source_model.reserve_slot(conn, self.claim, "unlaunched", now=NOW))
        self.sql("UPDATE request_events SET state='revoked' WHERE request_id='request';")
        with self.writer() as conn:
            self.assertFalse(source_model.authorize_launch(conn, self.claim, "unlaunched", launch_id="skip", now=NOW))
            self.assertTrue(source_model.finish(conn, self.claim, "unlaunched", outcome="interrupted", now=NOW))
            self.assertFalse(self.bind(conn, "next"))

    def test_other_native_rounds_compete_for_same_capacity(self):
        self.sql("""
            UPDATE review_process_pool SET max_concurrency=1;
            INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES('coordinator','foreign',1,'prepared');
            INSERT INTO review_sessions(round_id,repo,pr_number,workstream_id,trigger_fingerprint,task_id,
              request_json,created_at,updated_at) VALUES('review','org/else',1,'f','trigger','foreign','{}','now','now');
            INSERT INTO review_runs(run_id,round_id,stage,worker_id,attempt_no,owner_attempt_id,status,
              output_dir,prompt_sha256,created_at,updated_at)
              VALUES('reviewer','review','review','model',1,'coordinator','waiting','dir','hash','now','now');
        """)
        with self.writer() as conn:
            source_model.enqueue(
                conn,
                self.claim,
                run_id="source",
                round_id="round",
                request_id="request",
                stage="synthesis",
                worker_id="worker",
                input_version="v1",
                **self.packet("source"),
                now=NOW,
            )
            self.assertTrue(pool.reserve(conn, "review_runs", "reviewer", NOW))
            self.assertFalse(source_model.reserve_slot(conn, self.claim, "source", now=NOW))
            with self.assertRaisesRegex(ValueError, "source model slots"):
                pool.reserve(conn, "source_model_runs", "source", NOW)
        self.sql("UPDATE review_runs SET status='failed',slot_reserved=0;")
        with self.writer() as conn:
            self.assertTrue(source_model.reserve_slot(conn, self.claim, "source", now=NOW))
        self.sql("UPDATE review_runs SET status='waiting';")
        with self.writer() as conn:
            self.assertFalse(pool.reserve(conn, "review_runs", "reviewer", NOW))

    def test_migration_guard_refuses_granted_launch_and_invalid_exit_evidence(self):
        with self.writer() as conn:
            self.reserve_and_grant(conn, "active")

        def check_guard(blocked):
            with closing(sqlite3.connect(self.db)) as conn:
                if blocked:
                    with self.assertRaisesRegex(RuntimeError, "reconcile source model run exit"):
                        native_sources.check_quiescent(conn)
                else:
                    native_sources.check_quiescent(conn)

        check_guard(True)
        with self.writer() as conn:
            self.assertFalse(
                source_model.record_identity(
                    conn, self.claim, "active", launch_id="launch", identity={**IDENTITY, "pid": "bad"}, now=NOW
                )
            )
            self.assertTrue(
                source_model.record_identity(conn, self.claim, "active", launch_id="launch", identity=IDENTITY, now=NOW)
            )
            self.assertTrue(
                source_model.finish(
                    conn,
                    self.claim,
                    "active",
                    launch_id="launch",
                    outcome="failed",
                    exit_kind="group_exited",
                    identity=IDENTITY,
                    now=NOW,
                )
            )
        check_guard(False)
        self.sql("UPDATE source_model_runs SET exit_json='{}';")
        check_guard(True)
        with self.writer() as conn:
            self.assertEqual(reads.blockers(conn, "round")[0].reason, "model_exit_unconfirmed")
            self.assertFalse(self.finish(conn))

    def test_additive_schema_upgrade_preserves_queued_requests(self):
        self.sql("""
            DROP TABLE source_model_runs;
            DROP TABLE source_round_tasks;
            DROP TABLE source_round_requests;
            DROP TABLE source_rounds;
            DROP TABLE source_sessions;
        """)
        init_database(self.db)
        with self.writer() as conn:
            replacement = sessions.claim(conn, source_id="s", owner="new", now=NOW, lease_seconds=10)
            self.assertIsNotNone(self.start(conn, claim=replacement))
            self.assertEqual(len(reads.round_requests(conn, "round")), 1)

    def test_historical_reopened_task_and_unproved_process_block_new_round(self):
        with self.writer() as conn:
            self.assertTrue(self.bind(conn))
        self.sql("UPDATE tasks SET lifecycle='completed' WHERE task_id='task';")
        with self.writer() as conn:
            self.assertTrue(self.finish(conn))
        self.sql("UPDATE tasks SET lifecycle='queued' WHERE task_id='task';")
        with self.writer() as conn:
            self.assertIsNone(self.start(conn, round_id="next-round", members={"more": "v1"}))
        self.sql("""
            UPDATE tasks SET lifecycle='canceled' WHERE task_id='task';
            INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json)
                VALUES('revived','task',1,'canceled','{"dispatch":{"pid":123}}');
        """)
        with self.writer() as conn:
            self.assertIsNone(self.start(conn, round_id="next-round", members={"more": "v1"}))
        self.sql("""UPDATE attempts SET metadata_json='{"dispatch":{"pid":123},
            "process_stopped_at":"2026-11-14T14:30:03+00:00"}' WHERE attempt_id='revived';""")
        with self.writer() as conn:
            self.assertIsNotNone(self.start(conn, round_id="next-round", members={"more": "v1"}))

    def test_historical_unreconciled_effect_blocks_even_after_round_closed(self):
        with self.writer() as conn:
            self.assertTrue(self.bind(conn))
        self.sql("UPDATE tasks SET lifecycle='completed' WHERE task_id='task';")
        with self.writer() as conn:
            self.assertTrue(self.finish(conn))
        self.sql("""
            INSERT INTO github_actions(action_id,task_id,action_type,audit_status,created_at)
              VALUES('historical','task','comment','accepted','now');
            INSERT INTO publication_intents(publication_id,action_id,idempotency_key,request_fingerprint,status,created_at,updated_at)
              VALUES('unresolved','historical','key','fp','attention','now','now');
        """)
        with self.writer() as conn:
            blockers = reads.blockers_for_source(conn, "s")
            self.assertEqual([(row.kind, row.identity) for row in blockers], [("publication", "unresolved")])
            self.assertFalse(reads.blockers_for_source(conn, "other"))
            self.assertIsNone(self.start(conn, round_id="next-round", members={"more": "v1"}))
        self.sql("""
            UPDATE publication_intents SET status='published';
            UPDATE github_actions SET publish_status='published';
        """)
        with self.writer() as conn:
            self.assertIsNotNone(self.start(conn, round_id="next-round", members={"more": "v1"}))

    def test_pending_member_can_be_reprepared_once_previous_round_has_stopped(self):
        with self.writer() as conn:
            self.assertTrue(self.bind(conn))
        self.sql("""
            UPDATE tasks SET lifecycle='canceled' WHERE task_id='task';
            INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json)
              VALUES('attempt','task',1,'canceled','{"dispatch":{"pid":123}}');
        """)
        with self.writer() as conn:
            self.assertFalse(self.finish(conn))
            self.assertIsNone(self.start(conn, round_id="retry"))
        self.sql("""UPDATE attempts SET metadata_json='{"dispatch":{"pid":123},
            "process_stopped_at":"2026-11-14T14:30:01+00:00"}' WHERE attempt_id='attempt';""")
        with self.writer() as conn:
            self.assertTrue(self.finish(conn))
            self.assertIsNotNone(self.start(conn, round_id="retry"))
            self.assertEqual(reads.round_requests(conn, "round")[0]["request_id"], "request")
            self.assertEqual(reads.round_requests(conn, "retry")[0]["request_id"], "request")

    def test_prerelease_membership_rebuild_keeps_existing_model_fk_and_history(self):
        with self.writer() as conn:
            run = source_model.enqueue(
                conn,
                self.claim,
                run_id="kept",
                round_id="round",
                request_id="request",
                stage="identify",
                worker_id="worker",
                input_version="v1",
                prompt_sha256="b" * 64,
                now=NOW,
            )
            self.assertIsNotNone(run)
            self.assertTrue(source_model.finish(conn, self.claim, "kept", outcome="interrupted", now=NOW))
            self.assertTrue(self.finish(conn))
        self.sql("CREATE UNIQUE INDEX prerelease_request_unique ON source_round_requests(request_id);")
        init_database(self.db)
        with self.writer() as conn:
            self.assertEqual(reads.model_run(conn, "kept")["request_id"], "request")
            self.assertIsNotNone(self.start(conn, round_id="replacement"))
            self.assertEqual(reads.round_requests(conn, "round")[0]["content_version"], "v1")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_init_preserves_source_round_and_foreign_keys(self):
        init_database(self.db)
        with self.writer() as conn:
            self.assertEqual(reads.active_round(conn, "s")["round_id"], "round")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
