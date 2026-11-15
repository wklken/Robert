"""Run current persistence against a database made by the pre-ORM release."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from robert_agent.delivery.application import notifications
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import attempts
from robert_agent.persistence.rounds import review
from robert_agent.persistence.schema.database import init_database
from robert_agent.readmodels import summarize
from robert_agent.work import wakeup
from tests.support.database import connect as legacy_connect

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/persistence/pre_orm_5e0e952.sql"
_REPO = "repo:example/backend"
_STREAM = "github:example/backend#123"
_TASK = "task-11e41fa0f257"
_ATTEMPT = "attempt-b213140d8d0c"
_MIGRATION = "robert-native-sources-v1"
_NOW = "2026-11-07T19:58:00.120+08:00"


class PreOrmCutoverTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "pre-orm.sqlite3"
        # Keep fixture creation independent of the current initializer and mappings.
        with legacy_connect(self.db) as conn:
            conn.executescript(_FIXTURE.read_text(encoding="utf-8"))

    def test_upgrade_rebuilds_sources_and_preserves_relations_and_evidence(self):
        with legacy_connect(self.db) as conn:
            conn.execute(
                "UPDATE execution_subjects SET binding_status='attention' WHERE subject_id=?",
                (_TASK,),
            )
            conn.execute(
                "INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,"
                "consumed_event_fingerprints_json,metadata_json) VALUES ('old-result',?,?,"
                "'comment_analysis',?,'[\"comment:comment-1\"]','{ \"result_contract\": \"compact-v1\" }')",
                (_TASK, _ATTEMPT, _NOW),
            )
            conn.execute(
                "INSERT INTO github_actions(action_id,result_id,task_id,action_type,created_at,metadata_json) "
                "VALUES ('old-action','old-result',?,'comment',?,'{ \"audit\": \"old\" }')",
                (_TASK, _NOW),
            )
            conn.execute(
                "INSERT INTO publication_intents(publication_id,action_id,idempotency_key,request_fingerprint,status,"
                "created_at,updated_at) VALUES ('old-publication','old-action','key','fingerprint','published',?,?)",
                (_NOW, _NOW),
            )
        spool = self.db.parent / "legacy-result.json"
        spool.write_bytes(b'{"result_contract":"compact-v1"}')
        _, _, existing_markers = self._schema_and_markers()
        names = (
            "github_sources",
            "github_events",
            "workstreams",
            "workstream_sources",
            "work_items",
            "work_item_events",
            "tasks",
            "task_events",
            "attempts",
            "artifacts",
            "worker_results",
            "github_actions",
            "publication_intents",
            "audit_events",
        )
        before = self._rows(names)
        init_database(self.db)
        initialized = self._schema_and_markers()
        objects = {row[1] for row in initialized[0]}
        self.assertTrue({"sources", "source_events", "idx_source_events_recent"} <= objects)
        self.assertFalse({"github_sources", "github_events"} & objects)
        self.assertEqual(initialized[2][:-3], existing_markers)
        self.assertEqual(initialized[2][-3][1], _MIGRATION)
        after = self._rows(names[2:])
        action_index = names[2:].index("github_actions")
        self.assertEqual(after[:action_index], before[2 : 2 + action_index])
        self.assertEqual(after[action_index + 1 :], before[3 + action_index :])
        with legacy_connect(self.db) as conn:
            migrated_action = conn.execute(
                "SELECT action_id,result_id,task_id,action_type,target_url,external_id,audit_status,publish_status,"
                "created_at,metadata_json,provider,target_json FROM github_actions"
            ).fetchone()
            self.assertEqual(tuple(migrated_action), (*before[names.index("github_actions")][0], "github", None))
            source = conn.execute(
                "SELECT source_id,repo_id,source_key,source_type,number,html_url,title,state,author_login,"
                "source_updated_at,metadata_json,provider,host,project_key,external_id FROM sources"
            ).fetchone()
            self.assertEqual(tuple(source), (*before[0][0], "github", "github.com", "example/backend", "123"))
            event = conn.execute(
                "SELECT event_id,repo_id,source_id,event_fingerprint,event_type,actor_kind,actor_login,"
                "author_association,authorization_status,event_at,payload_json FROM source_events"
            ).fetchone()
            self.assertEqual(tuple(event), before[1][0])
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                conn.execute("SELECT binding_status FROM execution_subjects WHERE subject_id=?", (_TASK,)).fetchone()[
                    0
                ],
                "attention",
            )
            conn.execute("UPDATE attempts SET status='running' WHERE attempt_id=?", (_ATTEMPT,))
        init_database(self.db)
        self.assertEqual(self._schema_and_markers(), initialized)
        self.assertEqual(spool.read_bytes(), b'{"result_contract":"compact-v1"}')

    def test_upgrade_refuses_active_or_unproven_launches(self):
        cases = (
            ("running", "UPDATE attempts SET status='running' WHERE attempt_id=?", (_ATTEMPT,)),
            (
                "worker exit",
                "UPDATE attempts SET status='completed',metadata_json=? WHERE attempt_id=?",
                ('{"dispatch":{"pid":123,"process_exited":true}}', _ATTEMPT),
            ),
            (
                "launch reservation",
                "UPDATE execution_subjects SET metadata_json=? WHERE subject_id=?",
                (json.dumps({"dispatch_inflight": _ATTEMPT}), _TASK),
            ),
            (
                "review run",
                "INSERT INTO review_runs(run_id,round_id,stage,worker_id,attempt_no,owner_attempt_id,status,"
                "output_dir,prompt_sha256,created_at,updated_at,slot_reserved) "
                "VALUES ('model-run','review-round','review','worker',1,?,'failed','old-output','digest',?,?,1)",
                (_ATTEMPT, _NOW, _NOW),
            ),
            (
                "publish child",
                "INSERT INTO github_control_children(child_id,owner_id,lane,identity_json,status)"
                " VALUES ('child','owner','publish','{}','starting')",
                (),
            ),
            (
                "publication",
                "INSERT INTO publication_intents(publication_id,action_id,idempotency_key,"
                "request_fingerprint,status,created_at,updated_at) "
                "VALUES ('publication','action','key','fingerprint','executing',?,?)",
                (_NOW, _NOW),
            ),
        )
        for reason, statement, args in cases:
            with self.subTest(reason=reason):
                self.db = self.db.with_name(f"blocked-{reason}.sqlite3")
                with legacy_connect(self.db) as conn:
                    conn.executescript(_FIXTURE.read_text(encoding="utf-8"))
                    conn.execute(
                        "INSERT INTO review_sessions(round_id,repo,pr_number,workstream_id,trigger_fingerprint,"
                        "request_json,created_at,updated_at) VALUES ('review-round','example/backend',123,?,'event','{}',?,?)",
                        (_STREAM, _NOW, _NOW),
                    )
                    conn.execute(
                        "INSERT INTO github_actions(action_id,task_id,action_type,created_at) "
                        "VALUES ('action',?,'comment',?)",
                        (_TASK, _NOW),
                    )
                    conn.execute(statement, args)
                before = self._schema_and_markers()
                with self.assertRaisesRegex(RuntimeError, "source migration.*reconcile"):
                    init_database(self.db)
                self.assertEqual(self._schema_and_markers(), before)

    def test_legacy_model_status_alone_does_not_prove_exit(self):
        for has_dispatch_json in (True, False):
            with self.subTest(has_dispatch_json=has_dispatch_json):
                self.db = self.db.with_name(f"legacy-model-{has_dispatch_json}.sqlite3")
                with legacy_connect(self.db) as conn:
                    conn.executescript(_FIXTURE.read_text(encoding="utf-8"))
                    conn.execute("DROP TABLE review_runs")
                    suffix = ", dispatch_json TEXT" if has_dispatch_json else ""
                    conn.execute(f"CREATE TABLE review_runs(run_id TEXT PRIMARY KEY, status TEXT{suffix})")
                    if has_dispatch_json:
                        conn.execute(
                            "INSERT INTO review_runs VALUES('old-model','succeeded',?)",
                            (json.dumps({"pid": 123, "process_exited": True}),),
                        )
                    else:
                        conn.execute("INSERT INTO review_runs VALUES('old-model','succeeded')")
                before = self._schema_and_markers()
                with self.assertRaisesRegex(RuntimeError, "reconcile review_runs exit 'old-model'"):
                    init_database(self.db)
                self.assertEqual(self._schema_and_markers(), before)

    def test_integrity_failure_rolls_back_names_descendants_and_marker(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("UPDATE task_events SET event_id='missing'")
        before_schema = self._schema_and_markers()
        before = self._rows(("github_sources", "github_events", "task_events", "work_items"))
        with self.assertRaisesRegex(RuntimeError, "foreign key"):
            init_database(self.db)
        self.assertEqual(self._schema_and_markers(), before_schema)
        self.assertEqual(self._rows(("github_sources", "github_events", "task_events", "work_items")), before)

    def _rows(self, names):
        with legacy_connect(self.db) as conn:
            return tuple([tuple(row) for row in conn.execute(f"SELECT * FROM {name} ORDER BY 1")] for name in names)

    def _schema_and_markers(self):
        with legacy_connect(self.db) as conn:
            objects = [
                tuple(row)
                for row in conn.execute("SELECT type,name,tbl_name,rootpage,sql FROM sqlite_master ORDER BY type,name")
            ]
            versions = tuple(
                conn.execute(f"PRAGMA {name}").fetchone()[0]
                for name in (
                    "schema_version",
                    "user_version",
                    "application_id",
                )
            )
            markers = [
                tuple(row)
                for row in conn.execute(
                    "SELECT version,name,checksum,applied_at FROM schema_migrations ORDER BY version"
                )
            ]
        return objects, versions, markers

    def _attempt_evidence(self):
        with legacy_connect(self.db) as conn:
            row = conn.execute(
                "SELECT status,started_at,metadata_json FROM attempts WHERE attempt_id=?",
                (_ATTEMPT,),
            ).fetchone()
            return tuple(row)

    def _request_wakeup(self, conn, metadata):
        return wakeup.request_wakeup(
            conn,
            repo_id=_REPO,
            task_id=_TASK,
            reason="manual_operator_request",
            dedupe_key="cutover-compatibility",
            metadata=metadata,
            now=_NOW,
        )

    def test_current_read_write_preserves_legacy_encodings_after_upgrade(self):
        before = self._schema_and_markers()
        self.assertEqual([marker[0] for marker in before[2]], [1, 2, 3, 4, 5, 6])
        init_database(self.db)
        migrated = self._schema_and_markers()
        attempt = self._attempt_evidence()
        self.assertEqual(attempt[0], "prepared")
        self.assertEqual(summarize.summarize_database(self.db)["tasks"], 1)
        self.assertEqual(self._schema_and_markers(), migrated)

        metadata = {"note": "保留原编码", "counter": 2}
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            created = self._request_wakeup(conn, metadata)
        with legacy_connect(self.db) as legacy:
            row = legacy.execute(
                "SELECT task_id,status,not_before_at,metadata_json,typeof(metadata_json) "
                "FROM wakeups WHERE wakeup_id=?",
                (created["wakeup_id"],),
            ).fetchone()
            self.assertEqual(
                tuple(row),
                (
                    _TASK,
                    "pending",
                    _NOW,
                    '{"counter": 2, "note": "保留原编码"}',
                    "text",
                ),
            )
            legacy_json = '{ "counter" : 3, "note" : "旧驱动写入" }'
            legacy.execute(
                "UPDATE wakeups SET metadata_json=? WHERE wakeup_id=?",
                (
                    legacy_json,
                    created["wakeup_id"],
                ),
            )

        with closing(database.connect(self.db)) as conn, conn:
            existing = self._request_wakeup(conn, {"note": "duplicate request"})
        self.assertEqual(existing["wakeup_id"], created["wakeup_id"])
        self.assertEqual(existing["status"], "already_pending")
        self.assertEqual(existing["metadata"], {"counter": 3, "note": "旧驱动写入"})
        with legacy_connect(self.db) as legacy:
            self.assertEqual(
                [
                    tuple(row)
                    for row in legacy.execute(
                        "SELECT metadata_json,created_at,updated_at FROM wakeups WHERE dedupe_key='cutover-compatibility'"
                    )
                ],
                [(legacy_json, _NOW, _NOW)],
            )
            self.assertEqual(legacy.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self._attempt_evidence(), attempt)
        self.assertEqual(self._schema_and_markers(), migrated)

    def test_legacy_round_slot_claim_rolls_back_and_rechecks_waiting_status(self):
        with legacy_connect(self.db) as legacy:
            legacy.execute(
                "INSERT INTO review_sessions "
                "(round_id,repo,pr_number,workstream_id,trigger_fingerprint,request_json,created_at,updated_at) "
                "VALUES ('old-round','example/backend',123,?,'event','{}',?,?)",
                (_STREAM, _NOW, _NOW),
            )
            legacy.execute(
                "INSERT INTO review_runs "
                "(run_id,round_id,stage,worker_id,attempt_no,owner_attempt_id,status,output_dir,prompt_sha256,created_at,updated_at) "
                "VALUES ('old-run','old-round','review','worker',1,?,'waiting','old-output','digest',?,?)",
                (_ATTEMPT, _NOW, _NOW),
            )

        with (
            closing(database.connect(self.db)) as writer,
            legacy_connect(self.db) as observer,
        ):
            transactions.begin(writer, immediate=True)
            self.assertTrue(review.reserve_slot(writer, "old-run"))
            self.assertEqual(review.process_pool_status(writer), {"limit": 4, "in_use": 1})
            row = observer.execute("SELECT status,slot_reserved FROM review_runs WHERE run_id='old-run'").fetchone()
            self.assertEqual(tuple(row), ("waiting", 0))
            writer.rollback()
            self.assertEqual(review.process_pool_status(writer), {"limit": 4, "in_use": 0})

            with writer:
                transactions.begin(writer, immediate=True)
                self.assertTrue(review.reserve_slot(writer, "old-run"))
            row = observer.execute("SELECT status,slot_reserved FROM review_runs WHERE run_id='old-run'").fetchone()
            self.assertEqual(tuple(row), ("starting", 1))

        # The pool still has space. Refusal depends on this row's saved state.
        with closing(database.connect(self.db)) as contender, contender:
            transactions.begin(contender, immediate=True)
            self.assertFalse(review.reserve_slot(contender, "old-run"))
            self.assertEqual(review.process_pool_status(contender), {"limit": 4, "in_use": 1})

    def test_notification_delivery_keeps_repository_scope_optional(self):
        init_database(self.db)
        with legacy_connect(self.db) as legacy:
            legacy.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) "
                "VALUES ('foreign','example/foreign','bot','main','/foreign','/foreign/trees')"
            )
            legacy.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,created_at,updated_at) "
                "VALUES ('foreign-stream','foreign',?,?)",
                (_NOW, _NOW),
            )
            legacy.execute(
                "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) "
                "VALUES ('foreign-task','foreign-stream','completed',?,?)",
                (_NOW, _NOW),
            )
            legacy.executemany(
                "INSERT INTO notifications(notification_id,task_id,notification_type,channel,status,created_at) "
                "VALUES (?,?,?,'local','recorded',?)",
                [
                    (kind, task, kind, _NOW)
                    for kind, task in (
                        ("own", _TASK),
                        ("foreign", "foreign-task"),
                        ("global", None),
                    )
                ],
            )

        delivered = []

        def receive(_argv, *, input, **kwargs):
            delivered.append(json.loads(input)["type"])
            return SimpleNamespace(returncode=0)

        scoped = notifications.deliver_pending(
            self.db,
            command=["fixture-receiver"],
            runner=receive,
            now=_NOW,
            repo_ids=[_REPO],
        )
        self.assertEqual(scoped["sent"], 2)
        self.assertCountEqual(delivered, ["own", "global"])
        unscoped = notifications.deliver_pending(
            self.db,
            command=["fixture-receiver"],
            runner=receive,
            now=_NOW,
            repo_ids=None,
        )
        self.assertEqual(unscoped["sent"], 1)
        self.assertEqual(delivered[-1], "foreign")

    def test_latest_update_branch_uses_prior_task_when_newest_is_excluded(self):
        with legacy_connect(self.db) as legacy:
            legacy.executemany(
                "INSERT INTO tasks(task_id,workstream_id,lifecycle,route_id,created_at,updated_at) "
                "VALUES (?,?,'completed','update-existing-pr',?,?)",
                [(task, _STREAM, _NOW, _NOW) for task in ("prior", "latest", "empty")],
            )
            legacy.executemany(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,branch_name,finished_at) "
                "VALUES (?,?,1,'completed',?,?)",
                [
                    (f"{task}-attempt", task, branch, finished)
                    for task, branch, finished in (
                        ("prior", "codex/prior", "2026-11-06T09:00:00+00:00"),
                        ("latest", "codex/latest", "2026-11-06T10:00:00+00:00"),
                        ("empty", "", "2026-11-06T11:00:00+00:00"),
                    )
                ],
            )
        with closing(database.connect(self.db)) as conn:
            self.assertEqual(
                attempts.find_for_latest_workstream_update_branch(conn, _STREAM),
                [("codex/latest",)],
            )
            self.assertEqual(
                attempts.find_for_latest_workstream_update_branch(conn, _STREAM, exclude_task_id="latest"),
                [("codex/prior",)],
            )
