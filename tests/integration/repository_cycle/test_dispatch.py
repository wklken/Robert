from contextlib import closing
import sqlite3
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tests.support import PACKAGE_ROOT
from robert_agent.adapters.processes import worker as cycle_worker
from robert_agent.execution.application import dispatch as dispatch_queue
from robert_agent.execution import workspace as assigned_workspace
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
)


class CycleDispatchTests(RepositoryCycleFixture):
    def test_blocked_worktree_is_not_dispatched(self):
        from unittest.mock import patch
        from robert_agent.runtime import cycle as run_once

        with patch.object(assigned_workspace, 'prepare_worktree', return_value={
            "ok": False, "status": "blocked_dirty_worktree", "worktree_path": "/unmanaged",
        }):
            result = run_once.run_once(self.config_path, fixture_path=self.fixture_path,
                                       dry_run=True, skip_external=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["dispatch_count"], 0)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], "failed")

    def test_live_run_dispatches_existing_prepared_attempt(self):
        from robert_agent.runtime import cycle as run_once
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        old = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            attempt_id = conn.execute("SELECT attempt_id FROM attempts").fetchone()[0]
            conn.execute(
                "UPDATE attempts SET started_at = ?, heartbeat_at = ? WHERE attempt_id = ?",
                (old, old, attempt_id),
            )
        calls = []
        original_dispatch = cycle_worker.dispatch_worker

        def fake_dispatch_worker(**kwargs):
            calls.append(kwargs)
            return {
                "ok": True,
                "status": "running",
                "task_id": kwargs["task_id"],
                "attempt_id": kwargs["attempt_id"],
                "pid": 12345,
                "command": ["cbc", "-p"],
            }

        try:
            cycle_worker.dispatch_worker = fake_dispatch_worker
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            cycle_worker.dispatch_worker = original_dispatch

        self.assertTrue(second["ok"], second)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["dry_run"])
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            attempt_status, started_at, heartbeat_at = conn.execute(
                "SELECT status, started_at, heartbeat_at FROM attempts"
            ).fetchone()
        self.assertEqual(attempt_status, "running")
        self.assertGreater(datetime.fromisoformat(started_at), datetime.fromisoformat(old))
        self.assertGreater(datetime.fromisoformat(heartbeat_at), datetime.fromisoformat(old))

    def test_live_run_prepares_missing_worktree_before_dispatching_prepared_attempt(self):
        from robert_agent.runtime import cycle as run_once
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        replacement_worktree = self.worktree_root / "codex__dd-123-task"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_id = conn.execute("SELECT attempt_id FROM attempts").fetchone()[0]
            planned_worktree = Path(
                conn.execute("SELECT worktree_path FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()[0]
            )
        self.assertFalse(planned_worktree.exists())
        calls = []
        prepare_calls = []
        original_dispatch = cycle_worker.dispatch_worker
        original_prepare_worktree = assigned_workspace.prepare_worktree
        original_can_materialize_worktree = dispatch_queue.can_materialize_worktree

        def fake_prepare_worktree(repo, event, route_result, dry_run):
            prepare_calls.append(
                {
                    "dry_run": dry_run,
                    "event_fingerprint": event["event_fingerprint"],
                    "route_id": route_result["route_id"],
                }
            )
            self.assertFalse(dry_run)
            return {
                "worktree_path": str(replacement_worktree),
                "branch_name": "codex/dd-123-task",
            }

        def fake_dispatch_worker(**kwargs):
            calls.append(kwargs)
            return {
                "ok": True,
                "status": "running",
                "task_id": kwargs["task_id"],
                "attempt_id": kwargs["attempt_id"],
                "pid": 12345,
                "command": ["cbc", "-p"],
            }

        try:
            dispatch_queue.can_materialize_worktree = lambda _repo: True
            assigned_workspace.prepare_worktree = fake_prepare_worktree
            cycle_worker.dispatch_worker = fake_dispatch_worker
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            cycle_worker.dispatch_worker = original_dispatch
            assigned_workspace.prepare_worktree = original_prepare_worktree
            dispatch_queue.can_materialize_worktree = original_can_materialize_worktree

        self.assertTrue(second["ok"], second)
        self.assertEqual(
            prepare_calls,
            [
                {
                    "dry_run": False,
                    "event_fingerprint": "comment:comment-1",
                    "route_id": "new-pr",
                }
            ],
        )
        self.assertEqual(calls[0]["worktree_path"], str(replacement_worktree))
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt = conn.execute(
                "SELECT status, worktree_path, branch_name FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        self.assertEqual(attempt, ("running", str(replacement_worktree), "codex/dd-123-task"))

    def test_live_run_passes_configured_worker_command_to_dispatch(self):
        from robert_agent.runtime import cycle as run_once
        config = yaml.safe_load(self.config_path.read_text())
        config['workers']['default'].update(adapter='codex', command='/opt/homebrew/bin/codex')
        self.config_path.write_text(yaml.safe_dump(config))
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        old = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            attempt_id = conn.execute("SELECT attempt_id FROM attempts").fetchone()[0]
            conn.execute(
                "UPDATE attempts SET started_at = ?, heartbeat_at = ? WHERE attempt_id = ?",
                (old, old, attempt_id),
            )
        calls = []
        original_dispatch = cycle_worker.dispatch_worker

        def fake_dispatch_worker(**kwargs):
            calls.append(kwargs)
            return {
                "ok": True,
                "status": "running",
                "task_id": kwargs["task_id"],
                "attempt_id": kwargs["attempt_id"],
                "pid": 12345,
                "command": [kwargs["worker_command"], "-p"],
            }

        try:
            cycle_worker.dispatch_worker = fake_dispatch_worker
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            cycle_worker.dispatch_worker = original_dispatch

        self.assertTrue(second["ok"], second)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["worker_agent"], "codex")
        self.assertEqual(calls[0]["worker_command"], "/opt/homebrew/bin/codex")
        self.assertEqual(calls[0]["worker_agent_config"]["command_argv"], ["/opt/homebrew/bin/codex"])

    def test_live_dispatch_records_worker_log_artifacts(self):
        from robert_agent.runtime import cycle as run_once
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        stdout_path = self.data_dir / "artifacts" / "worker.stdout.log"
        stderr_path = self.data_dir / "artifacts" / "worker.stderr.log"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text("worker stdout", encoding="utf-8")
        stderr_path.write_text("worker stderr", encoding="utf-8")
        original_dispatch = cycle_worker.dispatch_worker

        def fake_dispatch_worker(**kwargs):
            return {
                "ok": True,
                "status": "running",
                "task_id": kwargs["task_id"],
                "attempt_id": kwargs["attempt_id"],
                "pid": 12345,
                "command": ["cbc", "-p"],
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
            }

        try:
            cycle_worker.dispatch_worker = fake_dispatch_worker
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            cycle_worker.dispatch_worker = original_dispatch

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            rows = conn.execute(
                """
                SELECT artifact_type, path
                FROM artifacts
                WHERE artifact_type IN ('worker_stdout', 'worker_stderr')
                ORDER BY artifact_type
                """
            ).fetchall()
        self.assertEqual(
            rows,
            [
                ("worker_stderr", str(stderr_path)),
                ("worker_stdout", str(stdout_path)),
            ],
        )

    def test_live_dispatch_failure_records_failed_state(self):
        from robert_agent.runtime import cycle as run_once
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        original_dispatch = cycle_worker.dispatch_worker

        def fake_dispatch_worker(**_kwargs):
            raise FileNotFoundError("cbc")

        try:
            cycle_worker.dispatch_worker = fake_dispatch_worker
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            cycle_worker.dispatch_worker = original_dispatch

        self.assertFalse(second["ok"], second)
        self.assertEqual(second["status"], "failed_dispatch")
        self.assertIn("cbc", second["safe_error"])
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            attempt_status, failure_json = conn.execute(
                "SELECT status, failure_json FROM attempts"
            ).fetchone()
            task_lifecycle = conn.execute("SELECT lifecycle FROM tasks").fetchone()[0]
            workstream_lifecycle, active_task_id = conn.execute(
                "SELECT lifecycle, active_task_id FROM workstreams"
            ).fetchone()
            run_status, error_json = conn.execute(
                """
                SELECT status, error_json
                FROM agent_runs
                WHERE run_id = ?
                """,
                (second["run_id"],),
            ).fetchone()
            dispatch_step = conn.execute(
                """
                SELECT status
                FROM run_steps
                WHERE run_id = ? AND step_key = 'dispatch'
                """,
                (second["run_id"],),
            ).fetchone()
            summarize_step = conn.execute(
                """
                SELECT status
                FROM run_steps
                WHERE run_id = ? AND step_key = 'summarize'
                """,
                (second["run_id"],),
            ).fetchone()
            notification = conn.execute(
                """
                SELECT notification_type, status
                FROM notifications
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
        self.assertEqual(attempt_status, "failed")
        self.assertIn("failed_dispatch", failure_json)
        self.assertEqual(task_lifecycle, "failed")
        self.assertEqual(workstream_lifecycle, "failed")
        self.assertIsNone(active_task_id)
        self.assertEqual(run_status, "failed")
        self.assertIn("failed_dispatch", error_json)
        self.assertEqual(dispatch_step, ("failed",))
        self.assertEqual(summarize_step, ("skipped",))
        self.assertEqual(notification, ("worker_dispatch_failed", "recorded"))
