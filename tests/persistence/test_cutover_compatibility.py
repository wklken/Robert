"""Run current persistence against a database made by the pre-ORM release."""

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from robert_agent.delivery.application import notifications
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import attempts
from robert_agent.persistence.rounds import review
from robert_agent.readmodels import summarize
from robert_agent.work import wakeup
from tests.support.database import connect as legacy_connect

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/persistence/pre_orm_5e0e952.sql"
_REPO = "repo:example/backend"
_STREAM = "github:example/backend#123"
_TASK = "task-11e41fa0f257"
_ATTEMPT = "attempt-b213140d8d0c"
_NOW = "2026-11-07T19:58:00.120+08:00"


class PreOrmCutoverTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "pre-orm.sqlite3"
        # Keep fixture creation independent of the current initializer and mappings.
        with legacy_connect(self.db) as conn:
            conn.executescript(_FIXTURE.read_text(encoding="utf-8"))

    def test_reinitialization_preserves_existing_attention_and_schema(
        self,
    ):
        from robert_agent.persistence.schema.database import init_database

        with legacy_connect(self.db) as conn:
            conn.execute(
                "UPDATE execution_subjects SET binding_status='attention' WHERE subject_id=?",
                (_TASK,),
            )
        existing_objects, _, existing_markers = self._schema_and_markers()
        init_database(self.db)
        initialized = self._schema_and_markers()
        self.assertLessEqual(set(existing_objects), set(initialized[0]))
        self.assertEqual(initialized[2], existing_markers)
        init_database(self.db)
        self.assertEqual(self._schema_and_markers(), initialized)
        with legacy_connect(self.db) as conn:
            rows = conn.execute(
                "SELECT binding_status FROM execution_subjects WHERE subject_type='task' AND subject_id=?",
                (_TASK,),
            ).fetchall()
            self.assertEqual([row[0] for row in rows], ["attention"])

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

    def test_current_read_write_needs_no_schema_change_and_remains_legacy_readable(
        self,
    ):
        before = self._schema_and_markers()
        self.assertEqual([marker[0] for marker in before[2]], [1, 2, 3, 4, 5, 6])
        attempt = self._attempt_evidence()
        self.assertEqual(attempt[0], "prepared")
        self.assertEqual(summarize.summarize_database(self.db)["tasks"], 1)
        self.assertEqual(self._schema_and_markers(), before)

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
        self.assertEqual(self._schema_and_markers(), before)

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
