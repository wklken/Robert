from contextlib import closing, contextmanager
from unittest.mock import patch
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import threading
from tempfile import TemporaryDirectory
import unittest

from robert_agent.adapters.git.source_evidence import FileEvidence
from robert_agent.adapters.processes.source_model import SourceModelOutcome, SourceModelProfile
from robert_agent.execution.application import source_models
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import source_sessions as read
from robert_agent.persistence.rounds import pool
from robert_agent.persistence.schema.database import init_database
from robert_agent.persistence.stores import source_sessions
from robert_agent.work import tapd_authority as tapd_facts
from robert_agent.work.application import tapd_intake

NOW = "2026-11-14T14:30:00+00:00"
IDENTITY = {
    "pid": 1234,
    "process_group_id": 1234,
    "identity_source": "proc",
    "process_start_token": "mock-start",
    "command_fingerprint": "e" * 64,
}


class SourceModelOrchestrationTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.db = Path(scratch.name) / "db.sqlite"
        init_database(self.db)
        self.profile = SourceModelProfile("test", Path("/controlled/claude"), "model", "high", 10, ())
        self.time = datetime.fromisoformat(NOW)
        self.evidence = FileEvidence("r", "README.md", "sha256:" + sha256(b"component").hexdigest(), "component")
        self.ticket = tapd_facts.TicketIdentity("tapd.example", "001", "story", "001")
        self.config = {
            "host": self.ticket.host,
            "bot_name": "helper",
            "reply_name": "helper",
            "api_timezone": "UTC",
            "projects": [
                {
                    "workspace_id": self.ticket.workspace_id,
                    "trusted_author": "trusted",
                    "rules": [],
                    "mentions": {"enabled": True, "activated_after": "2026-10-01T00:00:00+00:00"},
                    "repositories": ["org/repo"],
                }
            ],
        }
        body = "@helper review component"
        version = tapd_facts.CommentVersion(
            self.ticket, "stories", "17", "2026-11-14 13:00:00", None, sha256(body.encode()).hexdigest()
        )
        self.comments = (
            tapd_facts.CommentObservation(
                version,
                body,
                "trusted",
                tapd_facts.CreatorEvidence(version, "trusted", "creator"),
                tapd_facts.OwnBodyEvidence(version, "trusted", "own"),
            ),
        )
        self.sql("""
            INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root)
              VALUES('r','org/repo','bot','main','/repo','/trees');
        """)
        with self.writer() as conn:
            observed = self.observe(conn)
            self.request_id = observed.request_ids[0]
            self.claim = source_sessions.claim(
                conn, source_id=self.ticket.source_id, owner="a", now=NOW, lease_seconds=10
            )
            self.assertIsNotNone(self.start_round(conn, "round", observed.revision))

    def observe(self, conn, body="Inline bounded context"):
        facts = tapd_facts.TicketFacts(self.ticket, "Title", body, "open", (), (), False, ("closed",))
        observation = tapd_facts.TicketObservation(facts, self.now(), self.comments, "complete")
        return tapd_intake.observe_ticket(
            conn, self.config, observation, tapd_facts.PublicationFacts(self.ticket), now=self.now()
        )

    def start_round(self, conn, round_id, revision):
        return tapd_intake.admit_round(
            conn, self.config, self.ticket, self.claim, round_id=round_id, revision=revision, now=self.now()
        )

    def now(self):
        return self.time.isoformat()

    def sql(self, script):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(script)

    @contextmanager
    def writer(self):
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            yield conn

    def execute(self, runner, **options):
        arguments = {
            "round_id": "round",
            "request_id": self.request_id,
            "run_id": "run",
            "stage": "identify",
            "profile": self.profile,
            "evidence": (self.evidence,),
            "authorize": lambda *_: True,
            "now": self.now,
            "runner": runner,
        }
        return source_models.execute(self.db, self.claim, **{**arguments, **options})

    def selection(self, payload):
        value = json.loads(payload)
        self.assertIn("Inline bounded context", value.pop("prompt"))
        return json.dumps(
            {
                **value,
                "kind": "selection",
                "repositories": ["r"],
                "references": [
                    {
                        "repo_id": self.evidence.repo_id,
                        "path": self.evidence.path,
                        "revision": self.evidence.revision,
                    }
                ],
            }
        ).encode()

    def test_first_launch_refuses_changed_context_before_reserving_capacity(self):
        self.time += timedelta(seconds=1)
        with self.writer() as conn:
            self.observe(conn, body="Clearly changed input")
        result = self.execute(lambda *_: self.fail("unexpected stale launch"))
        self.assertEqual((result.status, result.reason), ("denied", "source_model_input_changed"))
        with self.writer() as conn:
            self.assertIsNone(read.model_run(conn, "run"))
            self.assertEqual(pool.status(conn)["in_use"], 0)

    def test_launch_grant_and_identity_are_durable_before_model_results(self):
        def runner(_profile, prompt, _stop, started):
            with self.writer() as conn:
                row = read.model_run(conn, "run")
                self.assertEqual(row["status"], "starting")
                self.assertEqual(row["launch_token"], self.claim.token)
                self.assertEqual(row["prompt_sha256"], json.loads(prompt)["prompt_sha256"])
            self.assertTrue(started(IDENTITY))
            return SourceModelOutcome("read", self.selection(prompt), "group_exited", IDENTITY)

        result = self.execute(runner)
        self.assertEqual(result.status, "succeeded")
        with self.writer() as conn:
            row = read.model_run(conn, "run")
            self.assertEqual(json.loads(row["output_json"])["repositories"], ["r"])
            self.assertEqual(pool.status(conn)["in_use"], 0)
            self.assertEqual(read.round_tasks(conn, "round"), [])

    def test_capacity_wait_reuses_exact_persisted_prompt_after_evidence_changes(self):
        with patch("robert_agent.execution.application.source_models.store.reserve_slot", return_value=False):
            result = self.execute(lambda *_: self.fail("no slot available"))
        self.assertEqual((result.status, result.reason), ("waiting", "model_capacity"))
        with self.writer() as conn:
            stored = read.model_run(conn, "run")
            packet = json.loads(stored["input_packet_json"])
            self.assertIn("component", packet["payload"])
            self.assertIsNone(stored["launch_id"])
        changed = FileEvidence("r", "README.md", "sha256:" + sha256(b"changed").hexdigest(), "changed")

        def runner(_profile, prompt, _stop, started):
            self.assertEqual(prompt, packet["payload"])
            self.assertTrue(started(IDENTITY))
            return SourceModelOutcome("read", self.selection(prompt), "group_exited", IDENTITY)

        self.assertEqual(self.execute(runner, evidence=(changed,)).status, "succeeded")

    def test_claim_check_renews_during_wait_and_observes_revocation(self):
        permit = True

        def runner(_profile, _prompt, stop, started):
            nonlocal permit
            self.assertTrue(started(IDENTITY))
            original_lease = read_lease()
            self.time += timedelta(seconds=3)
            self.assertFalse(stop.is_set())
            self.assertGreater(read_lease(), original_lease)
            permit = False
            self.assertTrue(stop.is_set())
            return SourceModelOutcome("stopped", exit_kind="group_exited", identity=IDENTITY)

        def read_lease():
            with self.writer() as conn:
                return read.session(conn, self.ticket.source_id)["lease_until"]

        with patch("robert_agent.execution.application.source_models.RECHECK_SECONDS", 0):
            result = self.execute(runner, authorize=lambda *_: permit)
        self.assertEqual(result.status, "interrupted")

    def test_unknown_start_and_takeover_reconcile_only_exact_identity(self):
        def runner(_profile, _prompt, _stop, started):
            self.assertTrue(started(IDENTITY))
            return SourceModelOutcome("process_attention")

        self.assertEqual(self.execute(runner).status, "attention")
        self.time += timedelta(seconds=31)
        with self.writer() as conn:
            replacement = source_sessions.claim(
                conn, source_id=self.ticket.source_id, owner="b", now=self.now(), lease_seconds=20
            )
            self.assertEqual(pool.status(conn)["in_use"], 1)
        self.assertEqual(
            source_models.recover(
                self.db, replacement, run_id="run", inspect_exit=lambda _: False, now=self.now
            ).status,
            "attention",
        )
        self.assertEqual(
            source_models.recover(self.db, self.claim, run_id="run", inspect_exit=lambda _: True, now=self.now).status,
            "attention",
        )
        result = source_models.recover(
            self.db, replacement, run_id="run", inspect_exit=lambda identity: identity == IDENTITY, now=self.now
        )
        self.assertEqual(result.status, "interrupted")
        with self.writer() as conn:
            self.assertEqual(pool.status(conn)["in_use"], 0)

    def test_missing_identity_keeps_slot_and_run_on_repeat(self):
        self.assertEqual(self.execute(lambda *_: SourceModelOutcome("process_attention")).status, "attention")
        self.assertEqual(self.execute(lambda *_: self.fail("second spawn")).status, "existing")
        self.assertEqual(
            source_models.recover(self.db, self.claim, run_id="run", inspect_exit=lambda _: True, now=self.now).status,
            "attention",
        )
        with self.writer() as conn:
            self.assertEqual(pool.status(conn)["in_use"], 1)

    def test_authority_revocation_cancels_output_and_releases_verified_group(self):
        permit = threading.Event()
        permit.set()

        def runner(_profile, prompt, _stop, started):
            self.assertTrue(started(IDENTITY))
            permit.clear()
            return SourceModelOutcome("read", self.selection(prompt), "group_exited", IDENTITY)

        self.assertEqual(self.execute(runner, authorize=lambda *_: permit.is_set()).status, "interrupted")
        with self.writer() as conn:
            row = read.model_run(conn, "run")
            self.assertIsNone(row["output_json"])
            self.assertEqual(pool.status(conn)["in_use"], 0)

    def test_oversized_context_and_synthesis_without_exact_children_never_launch(self):
        def no_run(*_):
            self.fail("unexpected launch")

        result = self.execute(no_run, stage="synthesis", synthesis_ready=lambda *_: True)
        self.assertEqual(result.status, "denied")
        with self.writer() as conn:
            self.assertTrue(
                source_sessions.finish_round(conn, self.claim, round_id="round", outcome="canceled", now=NOW)
            )
            self.time += timedelta(seconds=1)
            observed = self.observe(conn, body="x" * 50000)
            self.assertIsNotNone(self.start_round(conn, "huge", observed.revision))
        result = self.execute(no_run, round_id="huge")
        self.assertEqual(result.reason, "required_source_context_exceeds_capacity")
        with self.writer() as conn:
            self.assertIsNone(read.model_run(conn, "run"))

    def test_unaccepted_child_output_stays_out_of_synthesis(self):
        self.sql("""
            INSERT INTO workstreams(workstream_id,repo_id,created_at,updated_at) VALUES('w','r','now','now');
            INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES('child','w','queued','now','now');
        """)
        with self.writer() as conn:
            self.assertTrue(
                source_sessions.bind_task(
                    conn,
                    self.claim,
                    round_id="round",
                    request_id=self.request_id,
                    task_id="child",
                    capability="readonly",
                    now=NOW,
                )
            )
        self.sql("""
            UPDATE tasks SET lifecycle='completed' WHERE task_id='child';
            INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json) VALUES(
              'attempt','child',1,'completed','{"process_stopped_at":"2026-11-14T14:30:00+00:00"}');
            INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at)
              VALUES('accepted','child','attempt','local_result','now');
        """)
        content = {"answer": "observed"}
        receipt = {
            "task_id": "child",
            "repo_id": "r",
            "result_id": "accepted",
            "content": content,
            "result_sha256": sha256(json.dumps(content, sort_keys=True).encode()).hexdigest(),
        }
        unsupported = self.execute(
            lambda *_: self.fail("not accepted"),
            stage="synthesis",
            child_results=(receipt,),
            synthesis_ready=lambda *_: False,
        )
        self.assertEqual(unsupported.status, "denied")

        # A permissive callback cannot turn an unaccepted persisted native child into evidence.
        also_unsupported = self.execute(
            lambda *_: self.fail("unproved child"),
            stage="synthesis",
            child_results=(receipt,),
            synthesis_ready=lambda *_: True,
        )
        self.assertEqual(also_unsupported.status, "denied")
