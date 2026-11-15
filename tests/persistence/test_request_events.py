"""Source requests and scoped consumption survive schema cutover."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from robert_agent.persistence.schema.database import init_database


_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/persistence/pre_orm_5e0e952.sql"
_TASK = "task-11e41fa0f257"
_ATTEMPT = "attempt-b213140d8d0c"
_STREAM = "github:example/backend#123"
_EVENT = "event:comment:comment-1"
_NOW = "2026-11-14T03:00:00+00:00"


class RequestEventMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "old.sqlite3"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript(_FIXTURE.read_text())
            conn.execute("UPDATE task_events SET relationship='consumed' WHERE task_id=?", (_TASK,))
            for sibling, relationship in (("pending", "pending"), ("context", "context"), ("trigger", "trigger")):
                conn.execute(
                    "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at)"
                    " VALUES(?,?,'completed',?,?)",
                    (sibling, _STREAM, _NOW, _NOW),
                )
                conn.execute("INSERT INTO task_events VALUES(?,?,?,?)", (sibling, _EVENT, relationship, _NOW))
            conn.execute(
                "INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,"
                "consumed_event_fingerprints_json) VALUES('result',?,?,'comment_analysis',?,'[\"comment:comment-1\"]')",
                (_TASK, _ATTEMPT, _NOW),
            )

    def test_legacy_scopes_and_result_evidence_survive_repeated_init(self):
        init_database(self.db)
        for i in range(2):
            with self.subTest(initialization=i), closing(sqlite3.connect(self.db)) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT task_id,event_id,state,consumed_at,result_id FROM request_events ORDER BY task_id"
                    ).fetchall(),
                    [("pending", _EVENT, "pending", None, None), (_TASK, _EVENT, "consumed", _NOW, "result")],
                )
                self.assertEqual(
                    conn.execute("SELECT task_id,relationship FROM task_events ORDER BY task_id").fetchall(),
                    [("context", "context"), ("pending", "context"), (_TASK, "context"), ("trigger", "trigger")],
                )
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            init_database(self.db)

    def test_bad_legacy_membership_rolls_back_schema_and_both_migration_markers(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("UPDATE task_events SET event_id='missing' WHERE task_id='pending'")
        with self.assertRaisesRegex(RuntimeError, "foreign key.*request_events|foreign key.*task_events"):
            init_database(self.db)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertIsNone(
                conn.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name='request_events'").fetchone()
            )
            self.assertEqual(
                conn.execute("SELECT relationship FROM task_events WHERE task_id='pending'").fetchone(), ("pending",)
            )
            self.assertEqual(conn.execute("SELECT max(version) FROM schema_migrations").fetchone(), (6,))

    def test_missing_result_evidence_keeps_known_consumed_state_without_fake_time(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM worker_results WHERE result_id='result'")
        init_database(self.db)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT state,consumed_at,result_id FROM request_events WHERE task_id=?", (_TASK,)
                ).fetchone(),
                ("consumed", None, None),
            )


class RequestEventStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "new.sqlite3"
        init_database(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript("""
                PRAGMA foreign_keys=ON;
                INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root)
                  VALUES('r','o/r','bot','main','/repo','/trees');
                INSERT INTO workstreams(workstream_id,repo_id,created_at,updated_at) VALUES('w','r','now','now');
                INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at)
                  VALUES('current','w','running','now','now'),('prior','w','completed','now','now');
                INSERT INTO attempts(attempt_id,task_id,attempt_no,status)
                  VALUES('attempt','current',1,'completed'),('old-attempt','prior',1,'completed');
                INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at)
                  VALUES('result','current','attempt','comment_analysis','result-time'),
                        ('wrong','prior','old-attempt','comment_analysis','other-time');
                INSERT INTO sources(source_id,source_key,provider,host,project_key,source_type,external_id)
                  VALUES('source','tapd:k','tapd','tapd.example','p','story','001'),
                        ('other','tapd:other','tapd','tapd.example','p','story','002');
                INSERT INTO source_events(event_id,source_id,event_fingerprint,event_type,event_at,payload_json)
                  VALUES('remote','source','tapd:event','comment','2','{"body":"request"}'),
                        ('more','source','tapd:more','comment','3','{"body":"follow-up"}'),
                        ('context','source','tapd:context','comment','1','{"body":"earlier context"}'),
                        ('foreign','other','tapd:foreign','comment','4','{"body":"unrelated"}');
                """)

    def test_upgrade_from_generic_sources_refuses_live_worker_and_can_retry(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript("""
                DROP TABLE request_events;
                DROP TABLE ingress_requests;
                DROP TABLE task_events;
                CREATE TABLE task_events (
                  task_id TEXT NOT NULL REFERENCES tasks(task_id),
                  event_id TEXT NOT NULL REFERENCES source_events(event_id),
                  relationship TEXT NOT NULL CHECK (relationship IN ('trigger','pending','consumed','context')),
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(task_id,event_id)
                );
                DELETE FROM schema_migrations WHERE name='robert-request-event-consumption-v1';
                UPDATE attempts SET status='running' WHERE attempt_id='attempt';
                INSERT INTO task_events VALUES('current','remote','pending','now');
                """)
        with self.assertRaisesRegex(RuntimeError, "reconcile Worker attempt"):
            init_database(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT relationship FROM task_events").fetchone(), ("pending",))
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_schema WHERE name='request_events'").fetchone())
            conn.execute("UPDATE attempts SET status='completed' WHERE attempt_id='attempt'")
        init_database(self.db)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT state FROM request_events").fetchone(), ("pending",))
            self.assertEqual(conn.execute("SELECT relationship FROM task_events").fetchone(), ("context",))
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def enqueue(self, conn, *, event_ids=("remote",), **changes):
        from robert_agent.persistence.stores.ingress_requests import enqueue

        return enqueue(
            conn,
            **{
                "request_id": "request",
                "source_id": "source",
                "provider": "tapd",
                "auth_type": "mention",
                "authorization": {"identity": "mock-user"},
                "content_snapshot": {"body": "request"},
                "content_version": "v1",
                "event_ids": event_ids,
                "now": _NOW,
                **changes,
            },
        )

    def test_enqueue_requires_one_source_and_is_atomic_and_retryable_without_repo(self):
        from robert_agent.persistence import database
        from robert_agent.persistence.queries import ingress_requests, request_events

        with (
            closing(database.connect(self.db, foreign_keys=True)) as writer,
            closing(database.connect(self.db)) as reader,
        ):
            with self.assertRaisesRegex(ValueError, "belong"):
                self.enqueue(writer, event_ids=("remote", "foreign"))
            self.assertIsNone(ingress_requests.find_by_id(writer, "request"))
            self.assertEqual(request_events.for_ingress_request(writer, "request"), [])
            before_commit = self.enqueue(writer, event_ids=("remote", "more"))
            self.assertIsNone(before_commit["repo_id"])
            self.assertIsNone(before_commit["task_id"])
            self.assertIsNone(ingress_requests.find_by_id(reader, "request"))
            writer.rollback()
            self.assertIsNone(ingress_requests.find_by_id(writer, "request"))
            with writer:
                self.enqueue(writer, event_ids=("remote", "more"))
            self.assertEqual(self.enqueue(writer, event_ids=("more", "remote")), before_commit)
            with self.assertRaisesRegex(ValueError, "different observation"):
                self.enqueue(writer, event_ids=("remote",))
            self.assertEqual(
                [(r["event_id"], r["state"]) for r in request_events.for_ingress_request(reader, "request")],
                [("more", "pending"), ("remote", "pending")],
            )

    def test_task_pending_and_result_evidence_do_not_change_other_scopes(self):
        from robert_agent.persistence import database
        from robert_agent.persistence.queries import request_events, task_events
        from robert_agent.persistence.stores import request_events as write_ledger
        from robert_agent.persistence.stores import task_events as write_bindings

        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            self.enqueue(conn)
            write_bindings.bind_task_event(conn, "current", "context", "context", _NOW)
            write_bindings.bind_task_event(conn, "current", "more", "trigger", _NOW)
            write_bindings.append_task_event(conn, "current", "remote", "pending", _NOW)
            write_bindings.append_task_event(conn, "prior", "remote", "pending", _NOW)
            write_bindings.append_task_event(conn, "current", "context", "pending", _NOW)
            self.assertEqual(
                [row[1] for row in task_events.find_for_pending_events_for_task(conn, "current")],
                ["tapd:context", "tapd:event"],
            )
            handled = ["tapd:event", "tapd:foreign"]
            self.assertEqual(write_ledger.consume_task_result(conn, "current", handled, result_id="wrong", now=_NOW), 0)
            self.assertEqual(
                write_ledger.consume_task_result(conn, "current", handled, result_id="result", now=_NOW), 1
            )
            self.assertEqual(
                write_ledger.consume_task_result(conn, "current", handled, result_id="result", now="later"), 0
            )
            self.assertEqual(write_bindings.append_task_event(conn, "current", "remote", "pending", "later"), 0)
            self.assertEqual(
                [row[0] for row in task_events.find_for_events_for_task(conn, "current")],
                ["trigger", "context", "consumed"],
            )
            self.assertEqual(
                [row[1] for row in task_events.find_for_pending_events_for_task(conn, "prior")], ["tapd:event"]
            )
            self.assertEqual(request_events.for_ingress_request(conn, "request")[0]["state"], "pending")
            receipt = task_events.find_for_receipt_facts(conn, "remote", "current", "current")[0]
            self.assertEqual((receipt["task_id"], receipt["occurred_at"]), ("current", "result-time"))
