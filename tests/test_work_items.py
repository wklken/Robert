from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


def table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


class WorkItemSchemaTests(unittest.TestCase):
    def setUp(self):
        from robert_agent import storage

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "dd.sqlite3"
        storage.init_database(self.db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(self.conn.close)
        self.conn.execute(
            """
            INSERT INTO repos(
              repo_id, full_name, github_account, default_base_branch,
              repo_root, worktree_root, metadata_json
            ) VALUES ('repo-1', 'example/repo', 'robot', 'main', '/repo', '/worktrees', '{}')
            """
        )
        self.conn.execute(
            """
            INSERT INTO github_sources(
              source_id, repo_id, source_key, source_type, number, title, author_login
            ) VALUES ('source-1', 'repo-1', 'example/repo#1', 'issue', 1, 'Issue one', 'owner')
            """
        )
        self.conn.execute(
            """
            INSERT INTO workstreams(
              workstream_id, repo_id, primary_source_id, lifecycle, created_at, updated_at
            ) VALUES ('example/repo#1', 'repo-1', 'source-1', 'active', '2026-07-19T00:00:00+00:00', '2026-07-19T00:00:00+00:00')
            """
        )
        self.conn.commit()

    def _insert_item(self, **overrides):
        values = {
            "work_item_id": "wi-1",
            "repo_id": "repo-1",
            "title": "First item",
            "priority": "P2",
            "origin_type": "github",
            "origin_source_id": "source-1",
            "routing_mode": "auto",
            "requested_worker": None,
            "workstream_id": "example/repo#1",
            "creation_idempotency_key": "create-1",
            "created_by": "owner",
        }
        values.update(overrides)
        self.conn.execute(
            """
            INSERT INTO work_items(
              work_item_id, repo_id, title, priority, origin_type, origin_source_id,
              routing_mode, requested_worker, workstream_id, creation_idempotency_key,
              created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-07-19T00:00:00+00:00', '2026-07-19T00:00:00+00:00')
            """,
            tuple(values[key] for key in (
                "work_item_id", "repo_id", "title", "priority", "origin_type",
                "origin_source_id", "routing_mode", "requested_worker", "workstream_id",
                "creation_idempotency_key", "created_by",
            )),
        )

    def test_schema_creates_work_item_tables_and_control_columns(self):
        table_names = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertEqual(
            {"work_items", "work_item_events"},
            {name for name in table_names if name in {"work_items", "work_item_events"}},
        )
        self.assertIn("work_item_id", table_columns(self.conn, "wakeups"))
        self.assertIn("routing_mode", table_columns(self.conn, "tasks"))
        self.assertIn("requested_worker", table_columns(self.conn, "tasks"))

    def test_schema_enforces_work_item_identity_and_state_constraints(self):
        self._insert_item()
        self.conn.execute(
            """
            INSERT INTO work_item_events(
              event_id, work_item_id, event_type, actor_kind, actor_identity,
              idempotency_key, created_at
            ) VALUES ('event-1', 'wi-1', 'backfilled', 'system', 'migration', 'event-key', '2026-07-19T00:00:00+00:00')
            """
        )
        cases = [
            {"work_item_id": "wi-2", "creation_idempotency_key": "create-2"},
            {
                "work_item_id": "wi-3", "origin_source_id": None,
                "creation_idempotency_key": "create-1", "workstream_id": None,
                "origin_type": "web",
            },
            {
                "work_item_id": "wi-4", "origin_source_id": None,
                "creation_idempotency_key": "create-4", "origin_type": "web",
            },
            {
                "work_item_id": "wi-5", "origin_source_id": None,
                "creation_idempotency_key": "create-5", "workstream_id": None,
                "origin_type": "web", "priority": "urgent",
            },
            {
                "work_item_id": "wi-6", "origin_source_id": None,
                "creation_idempotency_key": "create-6", "workstream_id": None,
                "origin_type": "email",
            },
            {
                "work_item_id": "wi-7", "origin_source_id": None,
                "creation_idempotency_key": "create-7", "workstream_id": None,
                "origin_type": "web", "routing_mode": "manual",
            },
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(sqlite3.IntegrityError):
                self._insert_item(**values)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO work_item_events(
                  event_id, work_item_id, event_type, actor_kind, actor_identity,
                  idempotency_key, created_at
                ) VALUES ('event-2', 'wi-1', 'reply', 'operator', 'owner', 'event-key', '2026-07-19T00:00:01+00:00')
                """
            )


class WorkItemMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "legacy.sqlite3"

    def _create_legacy_database(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE schema_migrations (
                  version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                  checksum TEXT NOT NULL, applied_at TEXT NOT NULL
                );
                INSERT INTO schema_migrations VALUES (1, 'robert-initial-schema', 'stage-3-schema', datetime('now'));
                CREATE TABLE repos (
                  repo_id TEXT PRIMARY KEY, full_name TEXT NOT NULL UNIQUE, github_account TEXT NOT NULL,
                  default_base_branch TEXT NOT NULL, repo_root TEXT NOT NULL, worktree_root TEXT NOT NULL,
                  enabled INTEGER NOT NULL DEFAULT 1, metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE github_sources (
                  source_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE,
                  source_type TEXT NOT NULL, number INTEGER NOT NULL, html_url TEXT, title TEXT NOT NULL DEFAULT '',
                  state TEXT NOT NULL DEFAULT 'open', author_login TEXT, source_updated_at TEXT,
                  metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE workstreams (
                  workstream_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, primary_source_id TEXT,
                  origin_workstream_id TEXT, lifecycle TEXT NOT NULL DEFAULT 'active', active_task_id TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE tasks (
                  task_id TEXT PRIMARY KEY, workstream_id TEXT NOT NULL, lifecycle TEXT NOT NULL,
                  parent_task_id TEXT, priority TEXT NOT NULL DEFAULT 'P2', route_id TEXT,
                  expected_output TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE attempts (
                  attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, attempt_no INTEGER NOT NULL,
                  status TEXT NOT NULL, worktree_path TEXT, branch_name TEXT, started_at TEXT,
                  heartbeat_at TEXT, finished_at TEXT, failure_json TEXT,
                  metadata_json TEXT NOT NULL DEFAULT '{}', UNIQUE(task_id, attempt_no)
                );
                CREATE TABLE worker_results (
                  result_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                  output_type TEXT NOT NULL, consumed_event_fingerprints_json TEXT NOT NULL DEFAULT '[]',
                  verification_json TEXT NOT NULL DEFAULT '[]', handoff TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
                  UNIQUE(task_id, attempt_id)
                );
                CREATE TABLE wakeups (
                  wakeup_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, reason TEXT NOT NULL,
                  dedupe_key TEXT NOT NULL, task_id TEXT, attempt_id TEXT, result_id TEXT,
                  source_run_id TEXT, consumed_run_id TEXT, status TEXT NOT NULL DEFAULT 'pending',
                  not_before_at TEXT NOT NULL, expires_at TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  metadata_json TEXT NOT NULL DEFAULT '{}', UNIQUE(repo_id, reason, dedupe_key)
                );
                INSERT INTO repos VALUES ('repo-1', 'example/repo', 'robot', 'main', '/repo', '/worktrees', 1, '{}');
                INSERT INTO github_sources(
                  source_id, repo_id, source_key, source_type, number, title, author_login
                ) VALUES
                  ('issue-1', 'repo-1', 'example/repo#1', 'issue', 1, 'Root issue', 'owner'),
                  ('pr-2', 'repo-1', 'example/repo#2', 'pull_request', 2, 'Derived PR', 'robot');
                INSERT INTO workstreams VALUES
                  ('example/repo#1', 'repo-1', 'issue-1', NULL, 'completed', NULL,
                   '2026-07-18T00:00:00+00:00', '2026-07-19T00:00:00+00:00', '{}'),
                  ('example/repo#2', 'repo-1', 'pr-2', 'example/repo#1', 'completed', NULL,
                   '2026-07-18T01:00:00+00:00', '2026-07-19T00:00:00+00:00', '{}');
                INSERT INTO tasks VALUES (
                  'task-1', 'example/repo#1', 'completed', NULL, 'P1', 'comment-analysis',
                  'comment_analysis', '2026-07-18T00:00:00+00:00', '2026-07-19T00:00:00+00:00', '{}'
                );
                INSERT INTO attempts VALUES (
                  'attempt-1', 'task-1', 1, 'completed', NULL, NULL, NULL, NULL,
                  '2026-07-19T00:00:00+00:00', NULL, '{}'
                );
                INSERT INTO worker_results VALUES (
                  'result-1', 'task-1', 'attempt-1', 'comment_analysis', '[]', '[]', '',
                  '2026-07-19T00:00:00+00:00', '{}'
                );
                """
            )

    def test_upgrade_backfills_only_root_workstream_once_without_mutating_execution_rows(self):
        from robert_agent import storage

        self._create_legacy_database()
        storage.init_database(self.db_path)
        storage.init_database(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as conn:
            item = conn.execute(
                """
                SELECT work_item_id, title, origin_source_id, workstream_id, priority,
                       completed_at, created_by
                FROM work_items
                """
            ).fetchone()
            event_count = conn.execute("SELECT COUNT(*) FROM work_item_events").fetchone()[0]
            counts = tuple(
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("tasks", "attempts", "worker_results")
            )
            migration = conn.execute(
                "SELECT name, checksum FROM schema_migrations WHERE name = ?",
                (storage.WORK_ITEM_MIGRATION_NAME,),
            ).fetchall()

        self.assertEqual(item[0], storage._backfill_work_item_id("example/repo#1"))
        self.assertEqual(item[1:5], ("Root issue", "issue-1", "example/repo#1", "P1"))
        self.assertIsNotNone(item[5])
        self.assertEqual(item[6], "owner")
        self.assertEqual(event_count, 1)
        self.assertEqual(counts, (1, 1, 1))
        self.assertEqual(len(migration), 1)
        self.assertEqual(migration[0][1], storage.WORK_ITEM_MIGRATION_CHECKSUM)

    def test_failed_backfill_does_not_record_migration_marker(self):
        from robert_agent import storage

        self._create_legacy_database()
        with mock.patch.object(storage, "_backfill_work_items", side_effect=RuntimeError("stop")):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                storage.init_database(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as conn:
            marker_count = conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE name = ?",
                (storage.WORK_ITEM_MIGRATION_NAME,),
            ).fetchone()[0]
        self.assertEqual(marker_count, 0)


class WorkItemCommandTests(unittest.TestCase):
    def setUp(self):
        from robert_agent import storage
        from robert_agent.work_items import CommandContext

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "commands.sqlite3"
        storage.init_database(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                """
                INSERT INTO repos(
                  repo_id, full_name, github_account, default_base_branch,
                  repo_root, worktree_root, metadata_json
                ) VALUES ('repo-1', 'example/repo', 'robot', 'main', '/repo', '/worktrees', '{}')
                """
            )
        self.context = CommandContext(
            actor_kind="operator",
            actor_identity="owner",
            allowed_repo_ids=frozenset({"repo-1"}),
            allowed_workers=frozenset({"default", "reviewer"}),
        )

    def _create(self, **overrides):
        from robert_agent import work_items

        values = {
            "context": self.context,
            "repo_id": "repo-1",
            "title": "Add repository board",
            "description": "Build the control surface",
            "priority": "P1",
            "routing_mode": "auto",
            "requested_worker": None,
            "start": False,
            "idempotency_key": "create-1",
        }
        values.update(overrides)
        return work_items.create_work_item(self.db_path, **values)

    def _question(self, work_item_id, sequence):
        from robert_agent.work_items import record_system_event

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            task_id, workstream_id = conn.execute(
                """
                SELECT w.active_task_id, wi.workstream_id
                FROM work_items wi
                JOIN workstreams w ON w.workstream_id = wi.workstream_id
                WHERE wi.work_item_id = ?
                """,
                (work_item_id,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO attempts(
                  attempt_id, task_id, attempt_no, status, finished_at, metadata_json
                ) VALUES (?, ?, 1, 'completed', '2026-07-19T01:00:00+00:00', '{}')
                """,
                (f"attempt-{sequence}", task_id),
            )
            conn.execute(
                "UPDATE tasks SET lifecycle = 'waiting_for_user' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE workstreams SET lifecycle = 'waiting_for_user' WHERE workstream_id = ?",
                (workstream_id,),
            )
            event = record_system_event(
                conn,
                work_item_id,
                event_type="operator_question",
                idempotency_key=f"question-{sequence}",
                body=f"Question {sequence}?",
                metadata={"kind": "clarification", "task_id": task_id},
                now=f"2026-07-19T01:0{sequence}:00+00:00",
            )
        return event

    def test_create_backlog_and_replay_return_the_original_safe_result(self):
        first = self._create()
        second = self._create()

        self.assertEqual(second, first)
        self.assertEqual(first["item"]["title"], "Add repository board")
        self.assertIsNone(first["item"]["activated_at"])
        self.assertEqual(first["item"]["version"], 1)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM work_item_events").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM wakeups").fetchone()[0], 0)

    def test_create_and_start_materializes_one_local_task_and_wakeup(self):
        result = self._create(
            start=True,
            routing_mode="manual",
            requested_worker="reviewer",
        )

        item = result["item"]
        self.assertEqual(item["workstream_id"], f"local:{item['work_item_id']}")
        self.assertIsNotNone(item["activated_at"])
        with closing(sqlite3.connect(self.db_path)) as conn:
            task = conn.execute(
                "SELECT lifecycle, priority, routing_mode, requested_worker FROM tasks"
            ).fetchone()
            wakeup = conn.execute(
                "SELECT reason, work_item_id, task_id, status FROM wakeups"
            ).fetchone()
        self.assertEqual(task, ("detected", "P1", "manual", "reviewer"))
        self.assertEqual(wakeup[:2], ("manual_operator_request", item["work_item_id"]))
        self.assertIsNotNone(wakeup[2])
        self.assertEqual(wakeup[3], "pending")

    def test_validation_conflict_and_transaction_rollback_are_explicit(self):
        from robert_agent import work_items

        with self.assertRaises(work_items.WorkItemValidationError):
            self._create(repo_id="repo-2", idempotency_key="bad-repo")
        with self.assertRaises(work_items.WorkItemValidationError):
            self._create(
                routing_mode="manual",
                requested_worker="missing",
                idempotency_key="bad-worker",
            )
        with mock.patch.object(
            work_items.wakeup,
            "request_wakeup",
            side_effect=RuntimeError("wakeup failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "wakeup failed"):
                self._create(start=True, idempotency_key="rollback")
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0], 0)

        item = self._create()["item"]
        with self.assertRaises(work_items.WorkItemConflictError):
            work_items.execute_command(
                self.db_path,
                item["work_item_id"],
                context=self.context,
                command="edit",
                expected_version=99,
                idempotency_key="edit-stale",
                title="Changed",
            )

    def test_duplicate_command_replay_does_not_increment_version_or_add_wakeup(self):
        from robert_agent import work_items

        item = self._create()["item"]
        first = work_items.execute_command(
            self.db_path,
            item["work_item_id"],
            context=self.context,
            command="start",
            expected_version=1,
            idempotency_key="start-1",
        )
        second = work_items.execute_command(
            self.db_path,
            item["work_item_id"],
            context=self.context,
            command="start",
            expected_version=1,
            idempotency_key="start-1",
        )

        self.assertEqual(second, first)
        self.assertEqual(first["item"]["version"], 2)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM wakeups").fetchone()[0], 1)

    def test_two_waiting_reply_cycles_keep_one_item_and_one_serial_workstream(self):
        from robert_agent import work_items

        item = self._create(start=True)["item"]
        first_question = self._question(item["work_item_id"], 1)
        first_reply = work_items.execute_command(
            self.db_path,
            item["work_item_id"],
            context=self.context,
            command="reply",
            expected_version=item["version"],
            idempotency_key="reply-1",
            body="Use SQLite.",
        )
        second_question = self._question(item["work_item_id"], 2)
        second_reply = work_items.execute_command(
            self.db_path,
            item["work_item_id"],
            context=self.context,
            command="reply",
            expected_version=first_reply["item"]["version"],
            idempotency_key="reply-2",
            body="Proceed with the PR.",
        )

        self.assertEqual(first_reply["resolved_event_id"], first_question["event_id"])
        self.assertEqual(second_reply["resolved_event_id"], second_question["event_id"])
        self.assertGreater(second_reply["item"]["version"], first_reply["item"]["version"])
        with closing(sqlite3.connect(self.db_path)) as conn:
            item_count = conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
            task_rows = conn.execute(
                "SELECT task_id, parent_task_id, lifecycle FROM tasks ORDER BY created_at, task_id"
            ).fetchall()
            workstream_count = conn.execute("SELECT COUNT(*) FROM workstreams").fetchone()[0]
            running_attempts = conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE status = 'running'"
            ).fetchone()[0]
            active_tasks = conn.execute(
                """
                SELECT COUNT(*) FROM tasks
                WHERE lifecycle IN ('detected', 'authorized', 'classified', 'queued', 'running')
                """
            ).fetchone()[0]
        self.assertEqual(item_count, 1)
        self.assertEqual(len(task_rows), 3)
        self.assertIsNone(task_rows[0][1])
        self.assertEqual(task_rows[1][1], task_rows[0][0])
        self.assertEqual(task_rows[2][1], task_rows[1][0])
        self.assertEqual(workstream_count, 1)
        self.assertEqual(running_attempts, 0)
        self.assertEqual(active_tasks, 1)

    def test_approve_route_decision_resolves_attention_without_duplicate_task(self):
        from robert_agent import board, work_items

        now = "2026-07-19T01:00:00+00:00"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO github_sources(
                  source_id, repo_id, source_key, source_type, number, title, author_login
                ) VALUES (
                  'source-route-decision', 'repo-1', 'github:example/repo#7',
                  'issue', 7, 'Classify issue seven', 'owner'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id, lifecycle,
                  active_task_id, created_at, updated_at
                ) VALUES (
                  'github:example/repo#7', 'repo-1', 'source-route-decision',
                  'active', 'task-classify', ?, ?
                )
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks(
                  task_id, workstream_id, lifecycle, priority, route_id,
                  expected_output, created_at, updated_at
                ) VALUES (
                  'task-classify', 'github:example/repo#7', 'queued', 'P1',
                  'classification-result', 'classification_result', ?, ?
                )
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO attempts(
                  attempt_id, task_id, attempt_no, status, started_at, heartbeat_at
                ) VALUES (
                  'attempt-classify', 'task-classify', 1, 'running', ?, ?
                )
                """,
                (now, now),
            )
            item = work_items.ensure_github_work_item(
                conn,
                repo_id="repo-1",
                source_id="source-route-decision",
                workstream_id="github:example/repo#7",
                actor_identity="owner",
                route_confidence="low",
                now=now,
            )

        waiting = board.get_work_item_detail(self.db_path, item["work_item_id"])
        self.assertEqual(waiting["column"], "waiting")
        self.assertEqual(waiting["valid_commands"], ["approve"])

        approved = work_items.execute_command(
            self.db_path,
            item["work_item_id"],
            context=self.context,
            command="approve",
            expected_version=waiting["version"],
            idempotency_key="approve-route-decision",
        )

        detail = board.get_work_item_detail(self.db_path, item["work_item_id"])
        self.assertEqual(approved["resolved_event_id"], waiting["attention_signals"][0]["event_id"])
        self.assertIsNone(approved["task_id"])
        self.assertEqual(detail["column"], "doing")
        self.assertEqual(detail["attention_signals"], [])
        self.assertIsNotNone(detail["activated_at"])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)

    def test_cancel_rejects_a_live_attempt_without_mutation(self):
        from robert_agent import work_items

        item = self._create(start=True)["item"]
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            task_id = conn.execute("SELECT task_id FROM tasks").fetchone()[0]
            conn.execute(
                """
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at)
                VALUES ('attempt-running', ?, 1, 'running', '2026-07-19T01:00:00+00:00')
                """,
                (task_id,),
            )
            conn.execute("UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?", (task_id,))

        with self.assertRaises(work_items.WorkItemConflictError):
            work_items.execute_command(
                self.db_path,
                item["work_item_id"],
                context=self.context,
                command="cancel",
                expected_version=item["version"],
                idempotency_key="cancel-running",
            )
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertIsNone(
                conn.execute("SELECT canceled_at FROM work_items").fetchone()[0]
            )

    def test_github_issue_and_derived_pr_resolve_to_one_stable_item(self):
        from robert_agent import work_items

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                """
                INSERT INTO github_sources(
                  source_id, repo_id, source_key, source_type, number, title, author_login
                ) VALUES
                  ('issue-7', 'repo-1', 'github:example/repo#7', 'issue', 7, 'Fix issue seven', 'owner'),
                  ('pr-8', 'repo-1', 'github:example/repo!8', 'pull_request', 8, 'Fix issue seven', 'robot'),
                  ('pr-9', 'repo-1', 'github:example/repo!9', 'pull_request', 9, 'Independent PR', 'contributor')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id, origin_workstream_id,
                  lifecycle, created_at, updated_at
                ) VALUES
                  ('github:example/repo#7', 'repo-1', 'issue-7', NULL, 'active', '2026-07-19T00:00:00+00:00', '2026-07-19T00:00:00+00:00'),
                  ('github:example/repo!8', 'repo-1', 'pr-8', 'github:example/repo#7', 'active', '2026-07-19T00:01:00+00:00', '2026-07-19T00:01:00+00:00'),
                  ('github:example/repo!9', 'repo-1', 'pr-9', NULL, 'active', '2026-07-19T00:02:00+00:00', '2026-07-19T00:02:00+00:00')
                """
            )
            issue = work_items.ensure_github_work_item(
                conn,
                repo_id="repo-1",
                source_id="issue-7",
                workstream_id="github:example/repo#7",
                actor_identity="owner",
                route_confidence="high",
                now="2026-07-19T00:00:00+00:00",
            )
            replay = work_items.ensure_github_work_item(
                conn,
                repo_id="repo-1",
                source_id="issue-7",
                workstream_id="github:example/repo#7",
                actor_identity="owner",
                route_confidence="high",
                now="2026-07-19T00:00:01+00:00",
            )
            derived = work_items.ensure_github_work_item(
                conn,
                repo_id="repo-1",
                source_id="pr-8",
                workstream_id="github:example/repo!8",
                actor_identity="robot",
                route_confidence="high",
                now="2026-07-19T00:01:00+00:00",
            )
            independent = work_items.ensure_github_work_item(
                conn,
                repo_id="repo-1",
                source_id="pr-9",
                workstream_id="github:example/repo!9",
                actor_identity="contributor",
                route_confidence="high",
                now="2026-07-19T00:02:00+00:00",
            )
            resolved = work_items.resolve_work_item_for_workstream(
                conn,
                "github:example/repo!8",
            )
            counts = (
                conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM work_item_events").fetchone()[0],
            )

        self.assertEqual(issue["work_item_id"], replay["work_item_id"])
        self.assertEqual(issue["work_item_id"], derived["work_item_id"])
        self.assertEqual(issue["work_item_id"], resolved["work_item_id"])
        self.assertNotEqual(issue["work_item_id"], independent["work_item_id"])
        self.assertEqual(counts, (2, 2))


if __name__ == "__main__":
    unittest.main()
