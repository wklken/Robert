from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from robert_agent import storage
from tests import legacy_workflow_store as workflow_store
from tests import legacy_workflow_signals as workflow_signals
from robert_agent import sqlite_migration


class SQLiteMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "robert.db"
        storage.init_database(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('r','x/y','bot','main','/repo','/trees')"
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,lifecycle,active_task_id,created_at,updated_at) VALUES ('w','r','active','t','now','now')"
            )
            conn.execute(
                "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES ('t','w','queued','now','now')"
            )
            workflow_store.bind_subject(
                conn,
                "task",
                "t",
                "human_request",
                "dbos",
                "now",
                definition_version="v2",
            )

    def test_preview_is_read_only_and_apply_preserves_controls_and_fences_old_owner(
        self,
    ):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE workflow_bindings SET recovery_attempts=1, metadata_json=?",
                (json.dumps({"reason_code": "keep", "dispatch_history": "saved"}),),
            )
        before = self.db.read_bytes()
        plan = sqlite_migration.preview(self.db)
        self.assertTrue(plan["ok"], plan)
        self.assertEqual(before, self.db.read_bytes())
        result = sqlite_migration.apply(self.db, plan)
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            sqlite_migration.apply(self.db, plan)["status"], "already_applied"
        )
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT recovery_attempts FROM execution_subjects"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT binding_status FROM workflow_bindings").fetchone()[
                    0
                ],
                "terminal",
            )
            self.assertEqual(
                conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], "queued"
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM execution_migrations").fetchone()[0],
                1,
            )

    def test_stale_plan_rejected_without_handoff(self):
        plan = sqlite_migration.preview(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tasks SET updated_at='changed'")
        self.assertEqual(sqlite_migration.apply(self.db, plan)["status"], "stale_plan")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                conn.execute("SELECT binding_status FROM workflow_bindings").fetchone()[
                    0
                ],
                "active",
            )

    def test_running_worker_unknown_publication_and_v1_are_blockers(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES ('a','t',1,'running')"
            )
        self.assertFalse(sqlite_migration.preview(self.db)["ok"])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempts SET status='completed'")
            conn.execute("UPDATE workflow_bindings SET definition_version='v1'")
        self.assertFalse(sqlite_migration.preview(self.db)["ok"])

    def test_attention_remains_stopped(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE workflow_bindings SET binding_status='attention'")
        plan = sqlite_migration.preview(self.db)
        self.assertTrue(plan["ok"], plan)
        self.assertTrue(sqlite_migration.apply(self.db, plan)["ok"])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT binding_status FROM execution_subjects"
                ).fetchone()[0],
                "attention",
            )

    def test_unknown_signal_blocks_instead_of_acknowledging(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            binding = workflow_store.get_binding(conn, "task", "t")
            workflow_signals.record_subject_signal(
                conn,
                binding,
                "subject-events",
                "missing",
                {"fact_id": "missing"},
                "now",
            )
        plan = sqlite_migration.preview(self.db)
        self.assertFalse(plan["ok"])
        self.assertIn("unresolved_signal", {b["reason"] for b in plan["blockers"]})

    def test_missing_database_is_not_created(self):
        path = self.db.with_name("missing")
        with self.assertRaises(FileNotFoundError):
            sqlite_migration.preview(path)
        self.assertFalse(path.exists())

    def test_fresh_second_plan_is_also_idempotent(self):
        self.assertTrue(
            sqlite_migration.apply(self.db, sqlite_migration.preview(self.db))["ok"]
        )
        plan = sqlite_migration.preview(self.db)
        self.assertTrue(plan["ok"], plan)
        self.assertEqual(
            sqlite_migration.apply(self.db, plan)["status"], "already_migrated"
        )

    def test_rejected_spool_is_history_not_a_pending_result(self):
        directory = self.db.parent / (self.db.name + ".results")
        directory.mkdir()
        (directory / "rejected.json").write_text(json.dumps({"status": "rejected"}))
        self.assertTrue(sqlite_migration.preview(self.db)["ok"])
        (directory / "pending.json").write_text(json.dumps({"status": "pending"}))
        self.assertFalse(sqlite_migration.preview(self.db)["ok"])

    def test_completed_attempt_with_live_process_blocks_handoff(self):
        import subprocess
        import sys
        from robert_agent import worker_identity

        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        self.addCleanup(lambda: (child.terminate(), child.wait(timeout=5)))
        identity = worker_identity.capture_process_identity(child.pid)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json) VALUES ('done','t',1,'completed',?)",
                (json.dumps({"dispatch": identity}),),
            )
        plan = sqlite_migration.preview(self.db)
        self.assertFalse(plan["ok"], plan)
        self.assertTrue(
            any(b["reason"].startswith("worker_identity_") for b in plan["blockers"])
        )

    def test_unrelated_control_row_does_not_authorize_dbos_startup(self):
        from robert_agent import execution_store

        with closing(sqlite3.connect(self.db)) as conn, conn:
            execution_store.initialize(conn)
            execution_store.ensure_subject(conn, "task", "t", "human_request", "now")
            with self.assertRaisesRegex(RuntimeError, "offline migration"):
                execution_store.require_migrated(conn)

    def test_terminal_binding_nonterminal_task_is_a_blocker(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE workflow_bindings SET binding_status='terminal'")
        self.assertIn(
            "terminal_binding_nonterminal_domain",
            {b["reason"] for b in sqlite_migration.preview(self.db)["blockers"]},
        )

    def test_delivered_event_is_queued_with_original_payload_without_false_receipt(
        self,
    ):
        event = {
            "event_fingerprint": "reply",
            "workstream_id": "w",
            "actor_login": "trusted",
            "authorization_status": "authorized_trigger",
            "metadata": {"keep": "value"},
        }
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number) VALUES ('s','r','x/y#1','issue',1)"
            )
            conn.execute(
                "INSERT INTO github_events(event_id,repo_id,source_id,event_fingerprint,event_type,event_at,payload_json) VALUES ('e','r','s','reply','comment','now',?)",
                (json.dumps(event),),
            )
            binding = workflow_store.get_binding(conn, "task", "t")
            signal = workflow_signals.record_subject_signal(
                conn, binding, "subject-events", "reply", {"fact_id": "reply"}, "now"
            )
            conn.execute("UPDATE workflow_outbox SET status='delivered'")
        plan = sqlite_migration.preview(self.db)
        self.assertTrue(plan["ok"], plan)
        self.assertTrue(sqlite_migration.apply(self.db, plan)["ok"])
        with closing(sqlite3.connect(self.db)) as conn:
            row = conn.execute(
                "SELECT source_signal_id,topic,dedupe_key,payload_json,consumed_at FROM execution_pending_facts"
            ).fetchone()
            self.assertEqual(row[:3], (signal["signal_id"], "subject-events", "reply"))
            self.assertEqual(json.loads(row[3]), event)
            self.assertIsNone(row[4])
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM workflow_signal_receipts"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM wakeups").fetchone()[0], 1
            )

    def test_writer_lock_refuses_apply(self):
        plan = sqlite_migration.preview(self.db)
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            self.assertEqual(
                sqlite_migration.apply(self.db, plan)["status"], "database_busy"
            )

    def test_cli_preview_creates_private_plan_and_refuses_overwrite(self):
        from contextlib import redirect_stdout
        import io
        from robert_agent.cli.main import main

        path = self.db.with_name("plan.json")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "migrate",
                        "dbos",
                        "--db",
                        str(self.db),
                        "--dry-run",
                        "--plan-file",
                        str(path),
                        "--output",
                        "json",
                    ]
                ),
                0,
            )
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            sqlite_migration.run(self.db, plan_file=path)

    def test_conflicting_control_is_reported_before_apply(self):
        from robert_agent import execution_store

        with closing(sqlite3.connect(self.db)) as conn, conn:
            execution_store.ensure_subject(conn, "task", "t", "human_request", "now")
        plan = sqlite_migration.preview(self.db)
        self.assertFalse(plan["ok"])
        self.assertIn("execution_control_conflict", {b["reason"] for b in plan["blockers"]})
        self.assertFalse(sqlite_migration.apply(self.db, plan)["ok"])

    def test_running_standalone_cycle_blocks_handoff(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO agent_runs(run_id,status,started_at,config_path,dry_run) VALUES ('live','running','now','config',0)")
        plan = sqlite_migration.preview(self.db)
        self.assertFalse(plan["ok"])
        self.assertTrue(any(b.get("run_id") == "live" for b in plan["blockers"]), plan)

    def test_dispatch_lock_prevents_offline_apply(self):
        from robert_agent.publication_lock import task_publication_lock

        plan = sqlite_migration.preview(self.db)
        with task_publication_lock(self.db, "dispatch-admission") as acquired:
            self.assertTrue(acquired)
            self.assertEqual(sqlite_migration.apply(self.db, plan)["status"], "dispatch_in_progress")
        self.assertTrue(sqlite_migration.apply(self.db, plan)["ok"])

    def test_failed_handoff_rolls_back_and_can_retry_same_plan(self):
        from unittest.mock import patch
        from robert_agent import execution_store

        plan = sqlite_migration.preview(self.db)
        with patch.object(execution_store, "record_historical_facts", side_effect=RuntimeError("injected failure")):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                sqlite_migration.apply(self.db, plan)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT binding_status FROM workflow_bindings").fetchone()[0], "active")
            self.assertEqual(conn.execute("SELECT count(*) FROM execution_subjects").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM execution_migrations").fetchone()[0], 0)
        self.assertTrue(sqlite_migration.apply(self.db, plan)["ok"])
