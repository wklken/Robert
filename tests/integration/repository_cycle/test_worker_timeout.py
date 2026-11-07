from robert_agent.adapters.processes import identity as worker_identity
from contextlib import closing
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from tests.support import PACKAGE_ROOT
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
)


class WorkerTimeoutTests(RepositoryCycleFixture):
    def test_worker_timeout_seconds_caps_attempt_runtime(self):
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
        old = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
            conn.execute(
                "UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                """
                UPDATE attempts
                SET status = 'running', heartbeat_at = ?, started_at = ?, metadata_json = json_set(?, '$.result_contract', 'compact-v1')
                WHERE attempt_id = ?
                """,
                (
                    old,
                    old,
                    json.dumps(
                        {
                            "dispatch": {
                                **self._recorded_identity(12345),
                                "worker_launch": {"timeout_seconds": 60},
                            }
                        },
                        sort_keys=True,
                    ),
                    attempt_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES ('phase-worker-timeout', ?, 'prepare', 'running', 'worker started', '', ?)
                """,
                (attempt_id, old),
            )

        original_inspect = worker_identity.inspect_recorded_identity
        original_group = worker_identity.live_process_group
        original_terminate = worker_identity.terminate_recorded_identity
        try:
            # The fake PID must not inspect unrelated processes on the CI host.
            worker_identity.live_process_group = lambda _pid: False
            worker_identity.inspect_recorded_identity = lambda _identity: "alive_same"
            worker_identity.terminate_recorded_identity = lambda _identity: {
                "status": "terminated",
                "pid": 12345,
            }
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            worker_identity.live_process_group = original_group
            worker_identity.inspect_recorded_identity = original_inspect
            worker_identity.terminate_recorded_identity = original_terminate

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status, failure_json = conn.execute(
                "SELECT status, failure_json FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        self.assertEqual(attempt_status, "failed")
        self.assertEqual(json.loads(failure_json)["status"], "failed_timeout")
        self.assertEqual(json.loads(failure_json)["timeout_seconds"], 60)

    def test_stale_attempt_later_hard_timeout_signals_pid_and_releases_workstream(self):
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
        old = (datetime.now(timezone.utc) - timedelta(minutes=95)).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
            conn.execute(
                "UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                """
                UPDATE attempts
                SET status = 'stale', heartbeat_at = ?, started_at = ?, metadata_json = json_set(?, '$.result_contract', 'compact-v1')
                WHERE attempt_id = ?
                """,
                (
                    old,
                    old,
                    json.dumps(
                        {"dispatch": self._recorded_identity(12345)}, sort_keys=True
                    ),
                    attempt_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES ('phase-timeout', ?, 'prepare', 'running', 'worker started', '', ?)
                """,
                (attempt_id, old),
            )

        original_inspect = worker_identity.inspect_recorded_identity
        original_group = worker_identity.live_process_group
        original_terminate = worker_identity.terminate_recorded_identity
        try:
            # The fake PID must not inspect unrelated processes on the CI host.
            worker_identity.live_process_group = lambda _pid: False
            worker_identity.inspect_recorded_identity = lambda _identity: "alive_same"
            worker_identity.terminate_recorded_identity = lambda _identity: {
                "status": "terminated",
                "pid": 12345,
            }
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            worker_identity.live_process_group = original_group
            worker_identity.inspect_recorded_identity = original_inspect
            worker_identity.terminate_recorded_identity = original_terminate

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status = conn.execute(
                "SELECT status FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()[0]
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            workstream = conn.execute(
                "SELECT lifecycle, active_task_id FROM workstreams"
            ).fetchone()
            notification = conn.execute(
                "SELECT notification_type, status FROM notifications"
            ).fetchone()
        self.assertEqual(attempt_status, "failed")
        self.assertEqual(task_lifecycle, "failed")
        self.assertEqual(workstream, ("failed", None))
        self.assertEqual(notification, ("worker_timeout", "recorded"))
