from robert_agent.adapters.processes import attempt as attempt_process
from contextlib import closing
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from robert_agent.adapters.processes import worker as cycle_worker
from robert_agent.execution.application import dispatch as dispatch_queue
from robert_agent.execution.application import evidence
from robert_agent.execution.application import monitor
from robert_agent.work.application import preparation
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
)


class TransportRecoveryTests(RepositoryCycleFixture):
    def test_supervise_resumes_stale_command_snapshot_without_live_process(self):
        from unittest.mock import patch
        from robert_agent.runtime import cycle as run_once

        db_path, task_id, attempt_id = self._interrupted_attempt()
        with patch.object(attempt_process, "dispatch_status", return_value={"status": "dead"}):
            result = run_once.run_once(self.config_path, dry_run=True, skip_external=True)
        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn:
            attempts = conn.execute("SELECT status,worktree_path FROM attempts ORDER BY attempt_no").fetchall()
        self.assertEqual([row[0] for row in attempts], ["failed", "prepared"])
        self.assertEqual(attempts[0][1], attempts[1][1])

    def test_recovery_replays_deferred_command_completion_exactly_once(self):

        db_path, task_id, attempt_id = self._interrupted_attempt()
        command_dir = self.data_dir / "artifacts" / task_id
        command_dir.mkdir(parents=True, exist_ok=True)
        record_path = command_dir / f"{attempt_id}.verify.1.command.json"
        record_path.write_text(json.dumps({
            "task_id": task_id, "attempt_id": attempt_id, "phase": "verify",
            "status": "completed", "returncode": 0, "summary": "Command exited 0",
            "finished_at": datetime.now(timezone.utc).isoformat(), "snapshot_status": "deferred",
        }))
        with closing(sqlite3.connect(db_path)) as conn, conn:
            for _ in range(2):
                recovery = evidence.inspect_attempt_recovery(conn, attempt_id, task_id, None)
                self.assertEqual(recovery["status"], "recoverable_progress")
                self.assertEqual(recovery["commands"][0]["returncode"], 0)
            count = conn.execute(
                "SELECT COUNT(*) FROM worker_phases WHERE summary='Command exited 0'",
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_recovery_does_not_wait_on_reused_command_pid(self):
        import os

        db_path, task_id, attempt_id = self._interrupted_attempt()
        command_dir = self.data_dir / "artifacts" / task_id
        command_dir.mkdir(parents=True, exist_ok=True)
        (command_dir / f"{attempt_id}.verify.1.command.json").write_text(json.dumps({
            "task_id": task_id, "attempt_id": attempt_id, "phase": "verify",
            "status": "running", "pid": os.getpid(), "process_identity": "previous-incarnation",
        }))
        with closing(sqlite3.connect(db_path)) as conn:
            recovery = evidence.inspect_attempt_recovery(conn, attempt_id, task_id, None)
        self.assertEqual(recovery["status"], "recoverable_progress")

    def test_transport_failure_before_first_snapshot_resumes_with_backoff(self):
        from unittest.mock import patch
        from robert_agent.runtime import cycle as run_once

        db_path, task_id, attempt_id = self._interrupted_attempt(phase=False, transport=True)
        with patch.object(attempt_process, "dispatch_status", return_value={"status": "dead"}):
            result = run_once.run_once(self.config_path, dry_run=True, skip_external=True)
        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn:
            resume_id, metadata = conn.execute(
                "SELECT attempt_id,metadata_json FROM attempts ORDER BY attempt_no DESC LIMIT 1",
            ).fetchone()
            self.assertNotEqual(resume_id, attempt_id)
            resume = json.loads(metadata)["resume"]
            self.assertEqual(resume["transport_failure_count"], 1)
            self.assertGreater(datetime.fromisoformat(resume["not_before_at"]), datetime.now(timezone.utc))
            self.assertIsNone(dispatch_queue.current_dispatch_target(conn, resume_id))

    def test_transport_retry_exhaustion_reports_failure_without_new_attempt(self):
        from unittest.mock import patch
        from robert_agent.runtime import cycle as run_once

        db_path, task_id, attempt_id = self._interrupted_attempt(phase=False, transport=True)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json = json_set(?, '$.result_contract', 'compact-v1')", (json.dumps({
                "dispatch": self._recorded_identity(987654), "resume": {"transport_failure_count": 5},
            }),))
        with patch.object(attempt_process, "dispatch_status", return_value={"status": "dead"}):
            result = run_once.run_once(self.config_path, dry_run=True, skip_external=True)
        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
            failure = json.loads(conn.execute("SELECT failure_json FROM attempts").fetchone()[0])
            self.assertEqual(failure["status"], "failed_transport_retries_exhausted")
            self.assertEqual(conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], "failed")

    def test_transport_failure_detection_ignores_quoted_errors_and_recovered_turns(self):

        failed = json.dumps({"type": "turn.failed", "error": {"message": "stream disconnected before completion"}})
        quoted = json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "aggregated_output": failed,
        }})
        for tail in (quoted, failed + "\n" + json.dumps({"type": "turn.completed"}),
                     failed + "\n" + json.dumps({"type": "turn.failed", "error": {"message": "invalid credentials"}})):
            self.assertIsNone(evidence.terminal_transport_failure({"worker_stdout": {"tail": tail}}))

    def test_transport_backoff_expires_and_preserves_prompt_and_worktree(self):
        from unittest.mock import patch
        from robert_agent.runtime import cycle as run_once

        db_path, task_id, attempt_id = self._interrupted_attempt(phase=False, transport=True)
        with patch.object(attempt_process, "dispatch_status", return_value={"status": "dead"}):
            run_once.run_once(self.config_path, dry_run=True, skip_external=True)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            resume_id = conn.execute("SELECT attempt_id FROM attempts ORDER BY attempt_no DESC LIMIT 1").fetchone()[0]
            queue = []
            self.assertEqual(dispatch_queue.queue_prepared_attempts(conn, "repo:example/backend", {}, queue, 3), 0)
            conn.execute(
                "UPDATE attempts SET metadata_json=json_set(metadata_json,'$.resume.not_before_at',?) WHERE attempt_id=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), resume_id),
            )
            self.assertEqual(dispatch_queue.queue_prepared_attempts(conn, "repo:example/backend", {}, queue, 3), 1)
            target, worktree = dispatch_queue.current_dispatch_target(conn, resume_id)
            self.assertEqual(target["attempt_id"], resume_id)
            self.assertIn(resume_id, str(target["prompt_path"]))
            self.assertIsNotNone(worktree)

    def test_resume_dispatch_uses_attempt_prompt_despite_newer_old_artifact(self):
        from unittest.mock import patch
        from robert_agent.runtime import cycle as run_once

        db_path, task_id, attempt_id = self._interrupted_attempt(phase=False, transport=True)
        with patch.object(attempt_process, "dispatch_status", return_value={"status": "dead"}):
            run_once.run_once(self.config_path, dry_run=True, skip_external=True)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            resume_id = conn.execute("SELECT attempt_id FROM attempts ORDER BY attempt_no DESC LIMIT 1").fetchone()[0]
            conn.execute("UPDATE attempts SET metadata_json = json_set('{}', '$.result_contract', 'compact-v1') WHERE attempt_id=?", (resume_id,))
            conn.execute("UPDATE artifacts SET created_at='2099-01-01T00:00:00+00:00'"
                         " WHERE artifact_type='prompt' AND attempt_id=?", (attempt_id,))
            target, _ = dispatch_queue.current_dispatch_target(conn, resume_id)
            self.assertIn(resume_id, str(target["prompt_path"]))

    def test_immediate_transport_exit_enters_supervised_recovery(self):
        from unittest.mock import patch
        from robert_agent.runtime import cycle as run_once

        db_path, task_id, attempt_id = self._interrupted_attempt(phase=False, transport=True)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("UPDATE attempts SET status='prepared'")
            target, worktree = dispatch_queue.current_dispatch_target(conn, attempt_id)
        log = self.data_dir / "artifacts" / task_id / f"{attempt_id}.stdout.log"
        worker = {"name": "default", "command": "unused", "agent": "command",
                  "default_model": "default", "default_effort": "high"}
        with patch.object(dispatch_queue, 'ensure_dispatch_worktree', return_value=worktree), \
             patch.object(cycle_worker, "dispatch_worker", return_value={
                 "ok": False, "status": "failed_dispatch", "pid": 987654, "returncode": 1,
                 "stdout_path": str(log), "safe_error": "worker exited immediately with code 1",
             }):
            dispatched = dispatch_queue.dispatch_queued(
                db_path, ["repo:example/backend"],
                [{"task_info": target, "worktree_result": worktree,
                  "repo_id": "repo:example/backend", "repo": {}}],
                False, 3, workers=[worker],
            )
        self.assertTrue(dispatched[0]["ok"], dispatched)
        with patch.object(attempt_process, "dispatch_status", return_value={"status": "dead"}):
            result = run_once.run_once(self.config_path, dry_run=True, skip_external=True)
        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 2)
            metadata = json.loads(conn.execute(
                "SELECT metadata_json FROM attempts ORDER BY attempt_no DESC LIMIT 1",
            ).fetchone()[0])
            self.assertEqual(metadata["resume"]["transport_failure_count"], 1)

    def test_transport_failure_after_recorded_result_does_not_resume(self):
        from unittest.mock import patch
        from robert_agent.delivery.application import submission as worker_result

        db_path, task_id, attempt_id = self._interrupted_attempt(phase=False, transport=True)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("UPDATE tasks SET expected_output='local_result'")
            conn.execute("UPDATE route_decisions SET expected_output='local_result', route_id='local-result'")
        payload = worker_result.build_result(
            task_id, attempt_id, "local_result", planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"], verification=[], handoff="recorded",
            used_skills=[],
        )
        self.assertTrue(worker_result.record_result(db_path, payload)["ok"])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            with patch.object(attempt_process, "dispatch_status", return_value={"status": "dead"}):
                monitor.supervise_running_attempts(
                    conn, "repo:example/backend", {}, datetime.now(timezone.utc).isoformat(),
                    data_dir=self.data_dir, db_path=db_path, repo={"default_base_branch": "master"},
                                                             resume_attempt=preparation.create_resume_attempt_and_prompt,
                )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT status FROM attempts").fetchone()[0], "completed")

    def test_durable_command_is_startup_evidence_when_all_snapshot_writes_failed(self):
        import os
        from unittest.mock import patch
        from robert_agent.runtime import cycle as run_once
        from robert_agent.execution import command as heartbeat

        db_path, task_id, attempt_id = self._interrupted_attempt(phase=False)
        now = datetime.now(timezone.utc)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("UPDATE attempts SET started_at=?,heartbeat_at=?", (
                (now - timedelta(minutes=6)).isoformat(), now.isoformat(),
            ))
        command_dir = self.data_dir / "artifacts" / task_id
        command_dir.mkdir(parents=True, exist_ok=True)
        (command_dir / f"{attempt_id}.verify.1.command.json").write_text(json.dumps({
            "task_id": task_id, "attempt_id": attempt_id, "phase": "verify",
            "status": "running", "pid": os.getpid(),
            "process_identity": heartbeat.process_identity(os.getpid()),
        }))
        with patch.object(attempt_process, "dispatch_status", return_value={"status": "alive_same"}), \
             patch.object(attempt_process, "terminate_attempt", return_value={"status": "terminated"}):
            result = run_once.run_once(self.config_path, dry_run=True, skip_external=True)
        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM attempts").fetchone()[0], "running")

    def test_bound_transport_resume_keeps_worktree_and_stops_at_recovery_quota(self):
        from unittest.mock import patch
        from robert_agent.runtime import cycle as run_once

        db_path, task_id, attempt_id = self._interrupted_attempt(phase=False, transport=True)
        for expected_count, expected_delay in ((1, 30),):
            with patch.object(attempt_process, "dispatch_status", return_value={"status": "dead"}):
                result = run_once.run_once(self.config_path, dry_run=True, skip_external=True)
            self.assertTrue(result["ok"], result)
            with closing(sqlite3.connect(db_path)) as conn, conn:
                current_id, metadata, started_at, worktree = conn.execute(
                    "SELECT attempt_id,metadata_json,started_at,worktree_path FROM attempts"
                    " ORDER BY attempt_no DESC LIMIT 1",
                ).fetchone()
                resume = json.loads(metadata)["resume"]
                self.assertEqual(resume["transport_failure_count"], expected_count)
                self.assertEqual((datetime.fromisoformat(resume["not_before_at"]) -
                                  datetime.fromisoformat(started_at)).total_seconds(), expected_delay)
                previous_worktree = conn.execute(
                    "SELECT worktree_path FROM attempts WHERE attempt_id=?", (attempt_id,),
                ).fetchone()[0]
                self.assertEqual(worktree, previous_worktree)
                log = self.data_dir / "artifacts" / task_id / f"{current_id}.stdout.log"
                log.write_text(json.dumps({"type": "turn.failed", "error": {
                    "message": "stream disconnected before completion",
                }}) + "\n")
                conn.execute(
                    "INSERT INTO artifacts(artifact_id,task_id,attempt_id,artifact_type,path,created_at)"
                    " VALUES (?,?,?,'worker_stdout',?,?)",
                    (f"log-{expected_count}", task_id, current_id, str(log), started_at),
                )
                updated_metadata = json.loads(metadata)
                updated_metadata["dispatch"] = self._recorded_identity(987654)
                conn.execute("UPDATE attempts SET status='running',metadata_json = json_set(?, '$.result_contract', 'compact-v1') WHERE attempt_id=?",
                             (json.dumps(updated_metadata), current_id))

        with patch.object(attempt_process, "dispatch_status", return_value={"status": "dead"}):
            run_once.run_once(self.config_path, dry_run=True, skip_external=True)
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT binding_status FROM execution_subjects WHERE subject_id=?", (task_id,)).fetchone()[0], 'attention')

    def test_completed_command_does_not_hide_another_live_command(self):
        import os
        from robert_agent.execution import command as heartbeat

        db_path, task_id, attempt_id = self._interrupted_attempt()
        command_dir = self.data_dir / "artifacts" / task_id
        command_dir.mkdir(parents=True, exist_ok=True)
        # SQLite and artifacts can name the same directory differently on macOS.
        alias_dir = self.root / "command-alias"
        alias_dir.symlink_to(command_dir, target_is_directory=True)
        for index, details in enumerate((
            {"status": "completed", "returncode": 0},
            {"status": "running", "pid": os.getpid(), "process_identity": heartbeat.process_identity(os.getpid())},
        )):
            (command_dir / f"{attempt_id}.verify.{index}.command.json").write_text(json.dumps({
                "task_id": task_id, "attempt_id": attempt_id, "phase": "verify", **details,
            }))
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO artifacts(artifact_id,task_id,attempt_id,artifact_type,path,created_at)"
                " VALUES ('alias-prompt',?,?, 'prompt',?,?)",
                (task_id, attempt_id, str(alias_dir / "prompt.md"), datetime.now(timezone.utc).isoformat()),
            )
            recovery = evidence.inspect_attempt_recovery(conn, attempt_id, task_id, None)
        self.assertEqual(recovery["status"], "orphaned_command_running")
        self.assertEqual(len(recovery["commands"]), 2)
        self.assertEqual(
            {command["command_record_path"] for command in recovery["commands"]},
            {str(path.resolve()) for path in command_dir.glob("*.command.json")},
        )

    def test_recovery_replays_aliased_command_file_exactly_once(self):
        import hashlib

        db_path, task_id, attempt_id = self._interrupted_attempt()
        command_dir = self.data_dir / "artifacts" / task_id
        command_dir.mkdir(parents=True, exist_ok=True)
        custom_dir = self.root / "custom-command-output"
        custom_dir.mkdir()
        record_path = custom_dir / f"{attempt_id}.verify.1.command.json"
        record_path.write_text(json.dumps({
            "task_id": task_id, "attempt_id": attempt_id, "phase": "verify",
            "status": "completed", "returncode": 0, "summary": "Command exited 0",
            "finished_at": datetime.now(timezone.utc).isoformat(), "snapshot_status": "deferred",
        }))
        (command_dir / record_path.name).symlink_to(record_path)
        expected_phase_id = "phase-command-" + hashlib.sha256(str(record_path.resolve()).encode()).hexdigest()[:24]
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO artifacts(artifact_id,task_id,attempt_id,artifact_type,path,created_at)"
                " VALUES ('custom-command',?,?, 'worker_stdout',?,?)",
                (task_id, attempt_id, str(custom_dir / "stdout.log"), datetime.now(timezone.utc).isoformat()),
            )
            for _ in range(2):
                recovery = evidence.inspect_attempt_recovery(conn, attempt_id, task_id, None)
                self.assertEqual(len(recovery["commands"]), 1)
                self.assertEqual(recovery["status"], "recoverable_progress")
                self.assertEqual(recovery["commands"][0]["command_record_path"], str(record_path.resolve()))
                self.assertEqual(recovery["commands"][0]["process_status"], "finished")
            self.assertEqual(conn.execute(
                "SELECT phase_id FROM worker_phases WHERE summary='Command exited 0'",
            ).fetchall(), [(expected_phase_id,)])

    def test_transport_exhaustion_takes_precedence_over_hard_timeout(self):
        from unittest.mock import patch
        from robert_agent.runtime import cycle as run_once

        db_path, task_id, attempt_id = self._interrupted_attempt(phase=False, transport=True)
        old = (datetime.now(timezone.utc) - timedelta(minutes=95)).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("UPDATE attempts SET started_at=?,heartbeat_at=?,metadata_json = json_set(?, '$.result_contract', 'compact-v1')", (old, old, json.dumps({
                "dispatch": self._recorded_identity(987654), "resume": {"transport_failure_count": 5},
            })))
        with patch.object(attempt_process, "dispatch_status", return_value={"status": "dead"}):
            for _ in range(2):
                result = run_once.run_once(self.config_path, dry_run=True, skip_external=True)
                self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
            failure = json.loads(conn.execute("SELECT failure_json FROM attempts").fetchone()[0])
        self.assertEqual(failure["status"], "failed_transport_retries_exhausted")
