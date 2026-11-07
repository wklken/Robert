from robert_agent.adapters.processes import identity as worker_identity
from contextlib import closing
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tests.support import PACKAGE_ROOT
from robert_agent.execution.application import evidence
from robert_agent.execution.application import monitor
from robert_agent.work.application import preparation
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
)


class ResumePreparationTests(RepositoryCycleFixture):
    def test_supervise_prepares_resume_attempt_when_pid_is_gone_after_progress(self):
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
        stdout_path = self.data_dir / "artifacts" / "attempt.stdout.log"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(
            "I'm waiting on the background test run before I record the PR result.\n"
            "Command exited 0\n",
            encoding="utf-8",
        )
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id, worktree_path, branch_name = conn.execute(
                "SELECT task_id, attempt_id, worktree_path, branch_name FROM attempts"
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
                    json.dumps(
                        {
                            "dispatch": self._recorded_identity(987654),
                            "existing_pr_head_sha": "worker-base-sha",
                        }
                    ),
                    attempt_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES ('phase-command-done', ?, 'verify', 'completed', 'Command exited 0', 'Record result', ?)
                """,
                (attempt_id, now),
            )
            conn.execute(
                """
                INSERT INTO artifacts(
                  artifact_id, task_id, attempt_id, artifact_type, path, created_at
                )
                VALUES ('artifact-worker-stdout', ?, ?, 'worker_stdout', ?, ?)
                """,
                (task_id, attempt_id, str(stdout_path), now),
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
        second_now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            resume_attempt_id, resume_metadata_json = conn.execute(
                "SELECT attempt_id, metadata_json FROM attempts WHERE attempt_no = 2"
            ).fetchone()
            resume_metadata = json.loads(resume_metadata_json)
            resume_metadata["dispatch"] = self._recorded_identity(987655)
            conn.execute(
                "UPDATE attempts SET status = 'running', started_at = ?, heartbeat_at = ?, metadata_json = json_set(?, '$.result_contract', 'compact-v1') WHERE attempt_id = ?",
                (
                    second_now,
                    second_now,
                    json.dumps(resume_metadata, sort_keys=True),
                    resume_attempt_id,
                ),
            )
        original_inspect = worker_identity.inspect_recorded_identity
        original_recovery = evidence.inspect_attempt_recovery_with_metadata
        try:
            worker_identity.inspect_recorded_identity = (
                lambda _identity: "dead"
            )
            evidence.inspect_attempt_recovery_with_metadata = (
                lambda *_args, **_kwargs: {
                    "status": "recoverable_progress",
                    "task_id": task_id,
                    "previous_attempt_id": resume_attempt_id,
                    "progress_signals": ["dirty_worktree"],
                }
            )
            third = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=True,
            )
        finally:
            worker_identity.inspect_recorded_identity = original_inspect
            evidence.inspect_attempt_recovery_with_metadata = original_recovery

        self.assertTrue(third["ok"], third)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempts = conn.execute(
                """
                SELECT attempt_id, attempt_no, status, worktree_path, branch_name, metadata_json
                FROM attempts
                ORDER BY attempt_no
                """
            ).fetchall()
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            workstream = conn.execute(
                "SELECT lifecycle, active_task_id FROM workstreams"
            ).fetchone()
            artifacts = conn.execute(
                """
                SELECT artifact_type, path
                FROM artifacts
                WHERE task_id = ?
                ORDER BY created_at, artifact_type
                """,
                (task_id,),
            ).fetchall()
            notification = conn.execute(
                "SELECT notification_type, status FROM notifications WHERE notification_type = 'worker_identity_attention'"
            ).fetchone()
            binding = conn.execute(
                "SELECT binding_status FROM execution_subjects WHERE subject_type = 'task' AND subject_id = ?",
                (task_id,),
            ).fetchone()[0]

        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0][0], attempt_id)
        self.assertEqual(attempts[0][2], "failed")
        self.assertEqual(attempts[1][1:5], (2, "failed", worktree_path, branch_name))
        resumed_attempt_metadata = json.loads(attempts[1][5])
        resume_metadata = resumed_attempt_metadata["resume"]
        self.assertEqual(resume_metadata["previous_attempt_id"], attempt_id)
        self.assertEqual(
            resumed_attempt_metadata["existing_pr_head_sha"],
            "worker-base-sha",
        )
        self.assertEqual(task_lifecycle, "failed")
        self.assertEqual(workstream, ("failed", None))
        self.assertEqual(notification, ("worker_identity_attention", "recorded"))
        self.assertEqual(binding, "attention")
        artifact_types = [row[0] for row in artifacts]
        self.assertIn("recovery_context", artifact_types)
        prompt_path = Path([row[1] for row in artifacts if row[0] == "prompt"][-1])
        prompt = prompt_path.read_text(encoding="utf-8")
        self.assertIn("Resume Previous Attempt", prompt)
        self.assertIn(attempt_id, prompt)
        self.assertIn("Command exited 0", prompt)
        self.assertIn("git_status_short", prompt)

    def test_recovery_creation_failure_marks_attempt_and_binding_attention(self):
        from robert_agent.adapters.sqlite import execution_control as execution_store

        db_path, task_id, attempt_id = self._seed_active_pr_task(987)
        now = "2026-08-02T01:00:00+00:00"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE attempts SET status = 'running', started_at = ?, heartbeat_at = ?, metadata_json = json_set(?, '$.result_contract', 'compact-v1') WHERE attempt_id = ?",
                (
                    now,
                    now,
                    json.dumps({"dispatch": self._recorded_identity(987)}),
                    attempt_id,
                ),
            )
            execution_store.ensure_subject(conn, 'task', task_id, 'human_request', now)
            original_group = worker_identity.live_process_group
            worker_identity.live_process_group = lambda _pid: False
            original_inspect = worker_identity.inspect_recorded_identity
            original_recovery = evidence.inspect_attempt_recovery_with_metadata
            original_create = preparation.create_resume_attempt_and_prompt
            worker_identity.inspect_recorded_identity = (
                lambda _identity: "dead"
            )
            evidence.inspect_attempt_recovery_with_metadata = (
                lambda *_args, **_kwargs: {
                    "status": "recoverable_progress",
                    "task_id": task_id,
                    "previous_attempt_id": attempt_id,
                    "progress_signals": ["dirty_worktree"],
                }
            )
            preparation.create_resume_attempt_and_prompt = (
                lambda *_args, **_kwargs: None
            )
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
                    data_dir=self.data_dir,
                    db_path=db_path,
                    repo={},
                    resume_attempt=preparation.create_resume_attempt_and_prompt,
                )
            finally:
                worker_identity.live_process_group = original_group
                worker_identity.inspect_recorded_identity = original_inspect
                evidence.inspect_attempt_recovery_with_metadata = original_recovery
                preparation.create_resume_attempt_and_prompt = original_create
            binding = execution_store.get_binding(conn, "task", task_id)
            attempt_metadata = json.loads(
                conn.execute(
                    "SELECT metadata_json FROM attempts WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()[0]
            )
            notification = conn.execute(
                "SELECT notification_type, status FROM notifications"
            ).fetchone()

        self.assertEqual(supervised, 1)
        self.assertEqual(binding["binding_status"], "attention")
        self.assertEqual(
            attempt_metadata["supervise"]["reason"],
            "recovery_attempt_creation_failed",
        )
        self.assertEqual(notification, ("worker_identity_attention", "recorded"))

    def test_failed_timeout_recovery_creation_failure_marks_attention(self):
        from robert_agent.adapters.sqlite import execution_control as execution_store

        db_path, task_id, attempt_id = self._seed_active_pr_task(988)
        now = "2026-08-02T01:00:00+00:00"
        old = "2026-08-01T00:00:00+00:00"
        failure = {"status": "failed_timeout", "terminate": True}
        with closing(sqlite3.connect(db_path)) as conn, conn:
            execution_store.ensure_subject(conn, 'task', task_id, 'human_request', old)
            conn.execute(
                "UPDATE tasks SET lifecycle = 'failed' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE workstreams SET lifecycle = 'failed', active_task_id = NULL WHERE workstream_id = ?",
                ("github:example/backend!988",),
            )
            conn.execute(
                "UPDATE attempts SET status = 'failed', started_at = ?, heartbeat_at = ?, finished_at = ?, failure_json = ?, metadata_json = json_set(?, '$.result_contract', 'compact-v1') WHERE attempt_id = ?",
                (
                    old,
                    old,
                    old,
                    json.dumps(failure),
                    json.dumps({"dispatch": self._recorded_identity(988)}),
                    attempt_id,
                ),
            )
            original_inspect = worker_identity.inspect_recorded_identity
            original_group = worker_identity.live_process_group
            original_recovery = evidence.inspect_attempt_recovery_with_metadata
            original_create = preparation.create_resume_attempt_and_prompt
            worker_identity.inspect_recorded_identity = (
                lambda _identity: "dead"
            )
            # The synthetic PID must not observe an unrelated host process.
            worker_identity.live_process_group = lambda _pid: False
            evidence.inspect_attempt_recovery_with_metadata = (
                lambda *_args, **_kwargs: {
                    "status": "recoverable_progress",
                    "task_id": task_id,
                    "previous_attempt_id": attempt_id,
                    "progress_signals": ["dirty_worktree"],
                }
            )
            preparation.create_resume_attempt_and_prompt = (
                lambda *_args, **_kwargs: None
            )
            try:
                recovered = monitor.recover_failed_timeout_attempts(
                    conn,
                    "repo:example/backend",
                    now,
                    data_dir=self.data_dir,
                    db_path=db_path,
                    repo={},
                    resume_attempt=preparation.create_resume_attempt_and_prompt,
                )
            finally:
                worker_identity.inspect_recorded_identity = original_inspect
                worker_identity.live_process_group = original_group
                evidence.inspect_attempt_recovery_with_metadata = original_recovery
                preparation.create_resume_attempt_and_prompt = original_create
            binding = execution_store.get_binding(conn, "task", task_id)
            metadata = json.loads(
                conn.execute(
                    "SELECT metadata_json FROM attempts WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()[0]
            )

        self.assertEqual(recovered, 0)
        self.assertEqual(binding["binding_status"], "attention")
        self.assertEqual(
            metadata["supervise"]["reason"],
            "recovery_attempt_creation_failed",
        )

    def test_supervise_prepares_resume_attempt_when_timeout_pid_is_gone_after_progress(self):
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
        stdout_path = self.data_dir / "artifacts" / "attempt-timeout.stdout.log"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(
            "I have local changes and the verification command finished.\n"
            "Command exited 0\n",
            encoding="utf-8",
        )
        missing_pid = 987654
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id, worktree_path, branch_name = conn.execute(
                "SELECT task_id, attempt_id, worktree_path, branch_name FROM attempts"
            ).fetchone()
            conn.execute(
                "UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                """
                UPDATE attempts
                SET status = 'stale', started_at = ?, heartbeat_at = ?, metadata_json = json_set(?, '$.result_contract', 'compact-v1')
                WHERE attempt_id = ?
                """,
                (
                    old,
                    old,
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
                VALUES (
                  'phase-timeout-command-done', ?, 'verify', 'completed',
                  'Command exited 0', 'Record result', ?
                )
                """,
                (attempt_id, old),
            )
            conn.execute(
                """
                INSERT INTO artifacts(
                  artifact_id, task_id, attempt_id, artifact_type, path, created_at
                )
                VALUES ('artifact-timeout-worker-stdout', ?, ?, 'worker_stdout', ?, ?)
                """,
                (task_id, attempt_id, str(stdout_path), old),
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
            attempts = conn.execute(
                """
                SELECT
                  attempt_id, attempt_no, status, worktree_path, branch_name,
                  metadata_json, failure_json
                FROM attempts
                ORDER BY attempt_no
                """
            ).fetchall()
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            workstream = conn.execute(
                "SELECT lifecycle, active_task_id FROM workstreams"
            ).fetchone()
            notification = conn.execute(
                """
                SELECT notification_type, status
                FROM notifications
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()

        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0][0], attempt_id)
        self.assertEqual(attempts[0][2], "failed")
        failure = json.loads(attempts[0][6])
        self.assertEqual(failure["status"], "needs_resume")
        self.assertEqual(failure["original_status"], "failed_timeout")
        self.assertEqual(attempts[1][1:5], (2, "prepared", worktree_path, branch_name))
        resume_metadata = json.loads(attempts[1][5])["resume"]
        self.assertEqual(resume_metadata["previous_attempt_id"], attempt_id)
        self.assertEqual(task_lifecycle, "running")
        self.assertEqual(workstream, ("active", task_id))
        self.assertEqual(notification, ("worker_resume_prepared", "recorded"))

    def test_supervise_recovers_failed_timeout_attempt_when_pid_is_gone_after_progress(self):
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
        stdout_path = self.data_dir / "artifacts" / "failed-timeout.stdout.log"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(
            "The worker had local changes before timing out.\n"
            "Command exited 0\n",
            encoding="utf-8",
        )
        missing_pid = 987654
        failure = {
            "ok": False,
            "status": "failed_timeout",
            "terminate": True,
            "heartbeat_age_minutes": 95,
            "runtime_minutes": 95,
        }
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id, worktree_path, branch_name = conn.execute(
                "SELECT task_id, attempt_id, worktree_path, branch_name FROM attempts"
            ).fetchone()
            workstream_id = conn.execute(
                "SELECT workstream_id FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE tasks SET lifecycle = 'failed', updated_at = ? WHERE task_id = ?",
                (old, task_id),
            )
            conn.execute(
                """
                UPDATE attempts
                SET status = 'failed', started_at = ?, heartbeat_at = ?,
                    finished_at = ?, failure_json = ?, metadata_json = json_set(?, '$.result_contract', 'compact-v1')
                WHERE attempt_id = ?
                """,
                (
                    old,
                    old,
                    old,
                    json.dumps(failure, sort_keys=True),
                    json.dumps(
                        {"dispatch": self._recorded_identity(missing_pid)},
                        sort_keys=True,
                    ),
                    attempt_id,
                ),
            )
            conn.execute(
                """
                UPDATE workstreams
                SET lifecycle = 'failed', active_task_id = NULL, updated_at = ?
                WHERE workstream_id = ?
                """,
                (old, workstream_id),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES (
                  'phase-failed-timeout-command-done', ?, 'verify', 'completed',
                  'Command exited 0', 'Record result', ?
                )
                """,
                (attempt_id, old),
            )
            conn.execute(
                """
                INSERT INTO artifacts(
                  artifact_id, task_id, attempt_id, artifact_type, path, created_at
                )
                VALUES ('artifact-failed-timeout-worker-stdout', ?, ?, 'worker_stdout', ?, ?)
                """,
                (task_id, attempt_id, str(stdout_path), old),
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
            attempts = conn.execute(
                """
                SELECT
                  attempt_id, attempt_no, status, worktree_path, branch_name,
                  metadata_json, failure_json
                FROM attempts
                ORDER BY attempt_no
                """
            ).fetchall()
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            workstream = conn.execute(
                "SELECT lifecycle, active_task_id FROM workstreams"
            ).fetchone()
            notification = conn.execute(
                """
                SELECT notification_type, status
                FROM notifications
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
            supervise_output = conn.execute(
                """
                SELECT output_json
                FROM run_steps
                WHERE run_id = ? AND step_key = 'supervise'
                """,
                (second["run_id"],),
            ).fetchone()[0]

        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0][0], attempt_id)
        self.assertEqual(attempts[0][2], "failed")
        updated_failure = json.loads(attempts[0][6])
        self.assertEqual(updated_failure["status"], "needs_resume")
        self.assertEqual(updated_failure["original_status"], "failed_timeout")
        self.assertEqual(attempts[1][1:5], (2, "prepared", worktree_path, branch_name))
        resume_metadata = json.loads(attempts[1][5])["resume"]
        self.assertEqual(resume_metadata["previous_attempt_id"], attempt_id)
        self.assertEqual(task_lifecycle, "running")
        self.assertEqual(workstream, ("active", task_id))
        self.assertEqual(notification, ("worker_resume_prepared", "recorded"))
        supervise = json.loads(supervise_output)
        self.assertEqual(supervise["failed_timeout_recovered_count"], 1)
        self.assertEqual(supervise["supervised_count"], 1)
