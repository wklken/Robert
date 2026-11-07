from pathlib import Path
from tests.support.temp_workspace import TemporaryWorkspace


class WorkerDispatchTests(TemporaryWorkspace):
    def test_dispatch_dry_run_records_prepared_attempt(self):
        from robert_agent.adapters.processes import worker as dispatch

        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("hello", encoding="utf-8")
        result = dispatch.dispatch_worker(
            task_id="task-1",
            attempt_id="attempt-1",
            prompt_path=prompt_path,
            worktree_path=self.root / "worktree",
            dry_run=True,
        )

        self.assertEqual(result["status"], "prepared")
        self.assertIsNone(result["pid"])
        self.assertIn("command", result)
        self.assertEqual(result["prompt_path"], str(prompt_path))
        self.assertNotIn("hello", result["command"])

    def test_dispatch_detects_immediate_worker_exit_with_log_artifacts(self):
        from robert_agent.adapters.processes import worker as dispatch

        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("hello", encoding="utf-8")
        calls = []
        original_popen = dispatch.subprocess.Popen
        original_environment = dispatch._worker_environment
        original_cleanup = dispatch.worker_identity.cleanup_new_process
        cleaned = []

        class ExitedProcess:
            pid = 23456

            def poll(self):
                return 7

        def fake_worker_environment(allowed_names=()):
            return {"BASH_DEFAULT_TIMEOUT_MS": "300000", "WORKER_ENV": "set"}

        def fake_popen(command, **kwargs):
            calls.append((command, kwargs, kwargs["stdin"].read()))
            return ExitedProcess()

        try:
            dispatch.subprocess.Popen = fake_popen
            dispatch._worker_environment = fake_worker_environment
            dispatch.worker_identity.cleanup_new_process = lambda proc: (
                cleaned.append(proc.pid) or True
            )
            result = dispatch.dispatch_worker(
                task_id="task-1",
                attempt_id="attempt-1",
                prompt_path=prompt_path,
                worktree_path=self.root / "worktree",
                dry_run=False,
                startup_probe_seconds=0,
            )
        finally:
            dispatch.subprocess.Popen = original_popen
            dispatch._worker_environment = original_environment
            dispatch.worker_identity.cleanup_new_process = original_cleanup

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_dispatch")
        self.assertEqual(result["returncode"], 7)
        self.assertEqual(cleaned, [23456])
        self.assertFalse(result["cleanup_pending"])
        self.assertIn("stdout_path", result)
        self.assertIn("stderr_path", result)
        self.assertTrue(Path(result["stdout_path"]).exists())
        self.assertTrue(Path(result["stderr_path"]).exists())
        self.assertEqual(calls[0][1]["stdout"].name, result["stdout_path"])
        self.assertEqual(calls[0][1]["stderr"].name, result["stderr_path"])
        self.assertEqual(calls[0][1]["env"]["BASH_DEFAULT_TIMEOUT_MS"], "300000")
        self.assertEqual(calls[0][1]["env"]["WORKER_ENV"], "set")
        self.assertEqual(calls[0][1]["stdin"].name, str(prompt_path))
        self.assertEqual(calls[0][2], "hello")
        self.assertTrue(calls[0][1]["start_new_session"])

    def test_dispatch_reports_popen_failure_with_log_artifacts(self):
        from robert_agent.adapters.processes import worker as dispatch

        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("hello", encoding="utf-8")
        original_popen = dispatch.subprocess.Popen

        def fake_popen(_command, **_kwargs):
            raise FileNotFoundError("cbc")

        try:
            dispatch.subprocess.Popen = fake_popen
            result = dispatch.dispatch_worker(
                task_id="task-1",
                attempt_id="attempt-1",
                prompt_path=prompt_path,
                worktree_path=self.root / "worktree",
                dry_run=False,
                startup_probe_seconds=0,
            )
        finally:
            dispatch.subprocess.Popen = original_popen

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_dispatch")
        self.assertIn("cbc", result["safe_error"])
        self.assertTrue(Path(result["stdout_path"]).exists())
        self.assertTrue(Path(result["stderr_path"]).exists())

    def test_dispatch_records_observed_process_identity(self):
        from robert_agent.adapters.processes import worker as dispatch

        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("hello", encoding="utf-8")
        original_popen = dispatch.subprocess.Popen
        original_capture = dispatch.worker_identity.capture_process_identity

        class RunningProcess:
            pid = 23457

            def poll(self):
                return None

        def fake_popen(_command, **_kwargs):
            return RunningProcess()

        try:
            dispatch.subprocess.Popen = fake_popen
            dispatch.worker_identity.capture_process_identity = lambda _pid: {
                "pid": 23457,
                "process_state": "S",
                "process_group_id": 23457,
                "process_start_token": "start-token",
                "command_fingerprint": "a" * 64,
                "identity_source": "ps",
            }
            result = dispatch.dispatch_worker(
                task_id="task-identity",
                attempt_id="attempt-identity",
                prompt_path=prompt_path,
                worktree_path=self.root / "worktree",
                dry_run=False,
                startup_probe_seconds=0,
            )
        finally:
            dispatch.subprocess.Popen = original_popen
            dispatch.worker_identity.capture_process_identity = original_capture
            dispatch._BACKGROUND_PROCESSES.pop(23457, None)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["process_group_id"], 23457)
        self.assertEqual(result["process_start_token"], "start-token")
        self.assertEqual(result["identity_source"], "ps")

    def test_dispatch_cleans_up_when_identity_capture_fails(self):
        from robert_agent.adapters.processes import worker as dispatch

        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("hello", encoding="utf-8")
        original_popen = dispatch.subprocess.Popen
        original_capture = dispatch.worker_identity.capture_process_identity
        original_cleanup = dispatch.worker_identity.cleanup_new_process
        cleanup = []

        class RunningProcess:
            pid = 23458

            def poll(self):
                return None

        try:
            dispatch.subprocess.Popen = lambda _command, **_kwargs: RunningProcess()
            dispatch.worker_identity.capture_process_identity = lambda _pid: (
                _ for _ in ()
            ).throw(RuntimeError("identity unavailable"))
            dispatch.worker_identity.cleanup_new_process = lambda proc: cleanup.append(
                proc.pid
            )
            result = dispatch.dispatch_worker(
                task_id="task-identity-failed",
                attempt_id="attempt-identity-failed",
                prompt_path=prompt_path,
                worktree_path=self.root / "worktree",
                dry_run=False,
                startup_probe_seconds=0,
            )
        finally:
            dispatch.subprocess.Popen = original_popen
            dispatch.worker_identity.capture_process_identity = original_capture
            dispatch.worker_identity.cleanup_new_process = original_cleanup

        self.assertFalse(result["ok"], result)
        self.assertTrue(result["identity_capture_failed"])
        self.assertEqual(cleanup, [23458])
