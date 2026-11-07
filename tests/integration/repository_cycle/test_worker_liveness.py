from robert_agent.adapters.github import discovery as discover
from robert_agent.adapters.processes import identity as worker_identity
from contextlib import closing
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from tests.support import PACKAGE_ROOT
from robert_agent.adapters.processes import worker as cycle_worker
from robert_agent.execution.application import monitor
from robert_agent.work.application import preparation
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
)


class WorkerLivenessTests(RepositoryCycleFixture):
    def test_unknown_worker_identity_marks_binding_attention_without_mutation(self):
        from robert_agent.adapters.sqlite import execution_control as execution_store

        db_path, task_id, attempt_id = self._seed_active_pr_task(993)
        now = "2026-08-02T01:00:00+00:00"
        dispatch_metadata = {
            "pid": 43211,
            "process_group_id": 43211,
            "process_start_token": "start",
            "command_fingerprint": "a" * 64,
            "identity_source": "ps",
        }
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?", (task_id,))
            conn.execute(
                "UPDATE attempts SET status = 'running', started_at = ?, heartbeat_at = ?, metadata_json = json_set(?, '$.result_contract', 'compact-v1') WHERE attempt_id = ?",
                (now, now, json.dumps({"dispatch": dispatch_metadata}), attempt_id),
            )
            execution_store.ensure_subject(conn, 'task', task_id, 'human_request', now)
            original = worker_identity.inspect_recorded_identity
            worker_identity.inspect_recorded_identity = lambda _identity: "unknown"
            try:
                supervised = monitor.supervise_running_attempts(
                    conn,
                    "repo:example/backend",
                    {
                        "worker_startup_grace_seconds": 300,
                        "heartbeat_stale_seconds": 1200,
                        "hard_timeout_seconds": 5400,
                    },
                    now,
                    resume_attempt=preparation.create_resume_attempt_and_prompt,
                )
            finally:
                worker_identity.inspect_recorded_identity = original
            binding = execution_store.get_binding(conn, "task", task_id)
            attempt_state = conn.execute(
                "SELECT status FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()[0]

        self.assertEqual(supervised, 1)
        self.assertEqual(binding["binding_status"], "attention")
        self.assertEqual(attempt_state, "running")

    def test_supervise_marks_stale_running_attempt_and_notifies(self):
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
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
            conn.execute(
                "UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE attempts SET status = 'running', heartbeat_at = ?, started_at = ?, metadata_json = json_set(?, '$.result_contract', 'compact-v1') WHERE attempt_id = ?",
                (old, old, json.dumps({"dispatch": self._recorded_identity(987654)}), attempt_id),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES ('phase-stale', ?, 'prepare', 'running', 'worker started', '', ?)
                """,
                (attempt_id, old),
            )

        original_inspect = worker_identity.inspect_recorded_identity
        try:
            worker_identity.inspect_recorded_identity = (
                lambda _identity: "alive_same"
            )
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=True,
            )
        finally:
            worker_identity.inspect_recorded_identity = original_inspect

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status = conn.execute(
                "SELECT status FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()[0]
            notification = conn.execute(
                "SELECT notification_type, status FROM notifications"
            ).fetchone()
        self.assertEqual(attempt_status, "stale")
        self.assertEqual(notification, ("worker_stale", "recorded"))

    def test_supervise_retains_running_attempt_without_cleanup_pid(self):
        from robert_agent.runtime import cycle as run_once
        config_text = self.config_path.read_text(encoding="utf-8")
        self.config_path.write_text(
            config_text.replace(
                "  hard_timeout_seconds: 5400",
                "  hard_timeout_seconds: 5400\n  startup_grace_seconds: 30",
            ),
            encoding="utf-8",
        )
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
                "UPDATE attempts SET status = 'running', heartbeat_at = ?, started_at = ?, metadata_json = json_set(?, '$.result_contract', 'compact-v1') WHERE attempt_id = ?",
                (old, old, json.dumps({"dispatch": self._recorded_identity(987654)}), attempt_id),
            )

        original_inspect = worker_identity.inspect_recorded_identity
        original_terminate = worker_identity.terminate_recorded_identity
        try:
            worker_identity.inspect_recorded_identity = (
                lambda _identity: "alive_same"
            )
            worker_identity.terminate_recorded_identity = (
                lambda _identity: {"status": "attention", "reason": "invalid_identity"}
            )
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=True,
            )
        finally:
            worker_identity.inspect_recorded_identity = original_inspect
            worker_identity.terminate_recorded_identity = original_terminate

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status, failure_json = conn.execute(
                "SELECT status, failure_json FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            workstream_lifecycle = conn.execute(
                "SELECT lifecycle FROM workstreams"
            ).fetchone()[0]
            notification = conn.execute(
                "SELECT notification_type, status FROM notifications"
            ).fetchone()
        self.assertEqual(attempt_status, "running")
        self.assertIsNone(failure_json)
        self.assertEqual(task_lifecycle, "running")
        self.assertEqual(workstream_lifecycle, "active")
        self.assertEqual(notification, ("worker_cleanup_pending", "recorded"))

    def test_supervise_fails_running_attempt_when_dispatch_pid_is_gone(self):
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
        now = datetime.now(timezone.utc).isoformat()
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
                SET status = 'running', started_at = ?, heartbeat_at = ?, metadata_json = json_set(?, '$.result_contract', 'compact-v1')
                WHERE attempt_id = ?
                """,
                (
                    now,
                    now,
                    json.dumps({"dispatch": self._recorded_identity(987654)}),
                    attempt_id,
                ),
            )

        original_inspect = worker_identity.inspect_recorded_identity
        try:
            worker_identity.inspect_recorded_identity = (
                lambda _identity: "dead"
            )
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=True,
            )
        finally:
            worker_identity.inspect_recorded_identity = original_inspect

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status, failure_json = conn.execute(
                "SELECT status, failure_json FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            notification = conn.execute(
                "SELECT notification_type, status FROM notifications"
            ).fetchone()
        self.assertEqual(attempt_status, "failed")
        self.assertIn("failed_worker_process_exited", failure_json)
        self.assertEqual(task_lifecycle, "failed")
        self.assertEqual(notification, ("worker_process_exited", "recorded"))

    def test_supervise_waits_when_pid_is_gone_but_background_command_is_running(self):
        import sys
        from robert_agent.execution import command as heartbeat
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
        now = datetime.now(timezone.utc).isoformat()
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
                SET status = 'running', started_at = ?, heartbeat_at = ?, metadata_json = json_set(?, '$.result_contract', 'compact-v1')
                WHERE attempt_id = ?
                """,
                (
                    now,
                    now,
                    json.dumps({"dispatch": self._recorded_identity(987654)}),
                    attempt_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES ('phase-command-running', ?, 'verify', 'running', 'Command still running', 'Wait for command completion', ?)
                """,
                (attempt_id, now),
            )

        original_inspect = worker_identity.inspect_recorded_identity
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        command_dir = self.data_dir / "artifacts" / task_id
        command_dir.mkdir(parents=True, exist_ok=True)
        (command_dir / f"{attempt_id}.verify.1.command.json").write_text(json.dumps({
            "task_id": task_id, "attempt_id": attempt_id, "phase": "verify",
            "status": "running", "pid": proc.pid,
            "process_identity": heartbeat.process_identity(proc.pid),
        }))

        try:
            worker_identity.inspect_recorded_identity = (
                lambda _identity: "dead"
            )
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=True,
            )
        finally:
            worker_identity.inspect_recorded_identity = original_inspect

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempts = conn.execute(
                "SELECT attempt_id, status, metadata_json FROM attempts ORDER BY attempt_no"
            ).fetchall()
            notification_count = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0][1], "running")
        supervise_status = json.loads(attempts[0][2])["supervise"]["status"]
        self.assertEqual(supervise_status, "orphaned_command_running")
        self.assertEqual(notification_count, 0)

    def test_supervise_keeps_started_worker_with_prepare_snapshot_running(self):
        from robert_agent.runtime import cycle as run_once
        config_text = self.config_path.read_text(encoding="utf-8")
        self.config_path.write_text(
            config_text.replace(
                "  hard_timeout_seconds: 5400",
                "  hard_timeout_seconds: 5400\n  startup_grace_seconds: 30",
            ),
            encoding="utf-8",
        )
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
                "UPDATE attempts SET status = 'running', heartbeat_at = ?, started_at = ?, metadata_json = json_set(?, '$.result_contract', 'compact-v1') WHERE attempt_id = ?",
                (old, old, json.dumps({"dispatch": self._recorded_identity(987654)}), attempt_id),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES ('phase-prepare', ?, 'prepare', 'running', 'worker started', '', ?)
                """,
                (attempt_id, old),
            )

        original_inspect = worker_identity.inspect_recorded_identity
        try:
            worker_identity.inspect_recorded_identity = (
                lambda _identity: "alive_same"
            )
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=True,
            )
        finally:
            worker_identity.inspect_recorded_identity = original_inspect

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status, failure_json = conn.execute(
                "SELECT status, failure_json FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        self.assertEqual(attempt_status, "running")
        self.assertIsNone(failure_json)

    def test_supervise_restores_recovered_stale_attempt(self):
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
        now = datetime.now(timezone.utc)
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
                    now.isoformat(),
                    (now - timedelta(minutes=10)).isoformat(),
                    json.dumps({"dispatch": self._recorded_identity(987654)}),
                    attempt_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES ('phase-recovered', ?, 'prepare', 'running', 'worker started', '', ?)
                """,
                (attempt_id, (now - timedelta(minutes=10)).isoformat()),
            )

        original_inspect = worker_identity.inspect_recorded_identity
        try:
            worker_identity.inspect_recorded_identity = (
                lambda _identity: "alive_same"
            )
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=True,
            )
        finally:
            worker_identity.inspect_recorded_identity = original_inspect

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status = conn.execute(
                "SELECT status FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()[0]
        self.assertEqual(attempt_status, "running")

    def test_supervise_does_not_restore_stale_attempt_when_pid_is_gone(self):
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
        now = datetime.now(timezone.utc)
        missing_pid = 987654
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
                    now.isoformat(),
                    (now - timedelta(minutes=10)).isoformat(),
                    json.dumps(
                        {"dispatch": self._recorded_identity(missing_pid)},
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
                VALUES ('phase-recovered', ?, 'prepare', 'running', 'worker started', '', ?)
                """,
                (attempt_id, (now - timedelta(minutes=10)).isoformat()),
            )

        original_inspect = worker_identity.inspect_recorded_identity
        try:
            worker_identity.inspect_recorded_identity = (
                lambda _identity: "dead"
            )
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=True,
            )
        finally:
            worker_identity.inspect_recorded_identity = original_inspect

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status, failure_json, metadata_json = conn.execute(
                "SELECT status, failure_json, metadata_json FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            notification = conn.execute(
                "SELECT notification_type, status FROM notifications"
            ).fetchone()
        metadata = json.loads(metadata_json)
        failure = json.loads(failure_json)
        self.assertEqual(attempt_status, "failed")
        self.assertEqual(failure["status"], "failed_worker_process_exited")
        self.assertEqual(
            metadata["supervise"]["status"],
            "failed_worker_process_exited",
        )
        self.assertEqual(notification, ("worker_process_exited", "recorded"))

    def test_stale_attempt_retains_dispatch_capacity(self):
        from robert_agent.runtime import cycle as run_once
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                "max_concurrency: 3", "max_concurrency: 1"
            ),
            encoding="utf-8",
        )
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
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
                    json.dumps({"dispatch": self._recorded_identity(12345)}, sort_keys=True),
                    attempt_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES ('phase-capacity', ?, 'prepare', 'running', 'worker started', '', ?)
                """,
                (attempt_id, old),
            )
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-4",
                            "number": 124,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please analyze this",
                            "intent": "analysis",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        calls = []
        original_dispatch = cycle_worker.dispatch_worker
        original_inspect = worker_identity.inspect_recorded_identity

        def fake_dispatch_worker(**kwargs):
            calls.append(kwargs)
            return {
                "ok": True,
                "status": "running",
                **self._recorded_identity(123),
                "command": ["cbc"],
            }

        try:
            cycle_worker.dispatch_worker = fake_dispatch_worker
            worker_identity.inspect_recorded_identity = (
                lambda _identity: "alive_same"
            )
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                fixture_path=self.fixture_path,
                dry_run=False,
                skip_external=True,
            )
        finally:
            cycle_worker.dispatch_worker = original_dispatch
            worker_identity.inspect_recorded_identity = original_inspect

        self.assertTrue(second["ok"], second)
        self.assertEqual(len(calls), 0)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            statuses = conn.execute(
                "SELECT status FROM attempts ORDER BY started_at"
            ).fetchall()
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertCountEqual(statuses, [("stale",), ("prepared",)])
        self.assertEqual(task_count, 2)

    def test_live_discovery_failure_preserves_supervision_state(self):
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
                    json.dumps({"dispatch": self._recorded_identity(12345)}, sort_keys=True),
                    attempt_id,
                ),
            )

        original_inspect = worker_identity.inspect_recorded_identity
        original_group = worker_identity.live_process_group
        original_terminate = worker_identity.terminate_recorded_identity
        original_collect_notifications = discover.collect_account_notifications
        original_collect = discover.collect_live_events

        def fake_collect_notifications(_repos, runner=None):
            return {}

        def fake_collect(
            _repo,
            known_workstreams=None,
            notification_hints=None,
            include_notifications=True,
            **_kwargs,
        ):
            self.assertEqual(notification_hints, [])
            self.assertFalse(include_notifications)
            raise subprocess.CalledProcessError(1, ["gh", "search", "issues"])

        try:
            # The fake PID must not inspect unrelated processes on the CI host.
            worker_identity.live_process_group = lambda _pid: False
            worker_identity.inspect_recorded_identity = (
                lambda _identity: "alive_same"
            )
            worker_identity.terminate_recorded_identity = (
                lambda _identity: {"status": "terminated", "pid": 12345}
            )
            discover.collect_account_notifications = fake_collect_notifications
            discover.collect_live_events = fake_collect
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=False,
            )
        finally:
            worker_identity.live_process_group = original_group
            worker_identity.inspect_recorded_identity = original_inspect
            worker_identity.terminate_recorded_identity = original_terminate
            discover.collect_account_notifications = original_collect_notifications
            discover.collect_live_events = original_collect

        self.assertFalse(second["ok"], second)
        self.assertEqual(second["status"], "failed_discovery")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status = conn.execute(
                "SELECT status FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()[0]
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            run_status = conn.execute(
                "SELECT status FROM agent_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()[0]
            notification = conn.execute(
                "SELECT notification_type, status, task_id FROM notifications"
            ).fetchone()
        self.assertEqual(attempt_status, "failed")
        self.assertEqual(task_lifecycle, "failed")
        self.assertEqual(run_status, "failed")
        self.assertEqual(notification, ("worker_timeout", "recorded", task_id))
