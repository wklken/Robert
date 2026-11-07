from robert_agent.adapters.processes import attempt as attempt_process
from robert_agent.adapters.processes import identity as worker_identity
from contextlib import closing
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from tests import support  # noqa: F401
from robert_agent.adapters.processes import worker as dispatch
from robert_agent.persistence import execution_control as execution_store
from robert_agent.runtime import cycle as run_once
from robert_agent.execution import capacity


class LaunchOwnershipTests(unittest.TestCase):
    def test_failed_launch_exposes_unconfirmed_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "prompt.md"
            prompt.write_text("test")
            with (
                patch.object(dispatch.subprocess, "Popen", return_value=SimpleNamespace(pid=987654, poll=lambda: 2)),
                patch.object(dispatch.worker_identity, "cleanup_new_process", return_value=False),
            ):
                result = dispatch.dispatch_worker("task", "attempt", prompt, dry_run=False, startup_probe_seconds=0)
            self.assertTrue(result["cleanup_pending"])
            self.assertTrue(result["process_exited"])

    def test_cleanup_attention_can_only_be_canceled_after_group_is_gone(self):
        metadata = {"dispatch": {"pid": 987654, "cleanup_pending": True}}
        with patch.object(
            worker_identity,
            "terminate_recorded_identity",
            side_effect=AssertionError("unverified PID must not be signaled"),
        ):
            with patch.object(worker_identity, "live_process_group", return_value=True):
                self.assertEqual(attempt_process.terminate_attempt(metadata)["status"], "attention")
            with patch.object(worker_identity, "live_process_group", return_value=False):
                self.assertEqual(attempt_process.terminate_attempt(metadata)["status"], "already_dead")

    def test_failed_leader_cleans_up_term_resistant_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.md"
            prompt.write_text("test")
            ready = root / "ready"
            child = (
                "import signal,time; from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"Path({str(ready)!r}).touch(); time.sleep(60)"
            )
            leader = (
                "import subprocess,sys,time; from pathlib import Path; "
                f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
                f"ready=Path({str(ready)!r})\n"
                "while not ready.exists(): time.sleep(.01)\n"
                "sys.exit(2)"
            )
            popen = subprocess.Popen
            processes = []

            def launch(*args, **kwargs):
                proc = popen(*args, **kwargs)
                if kwargs.get("start_new_session"):
                    processes.append(proc)
                    proc.wait(timeout=5)  # Deterministic startup-probe failure.
                return proc

            try:
                with patch.object(dispatch.subprocess, "Popen", side_effect=launch):
                    result = dispatch.dispatch_worker(
                        "task",
                        "attempt",
                        prompt,
                        worker_agent_config={"agent": "command", "command_argv": [sys.executable, "-c", leader]},
                        dry_run=False,
                        startup_probe_seconds=0,
                    )
                self.assertFalse(result["ok"])
                self.assertFalse(worker_identity.live_process_group(result["pid"]))
                self.assertFalse(result.get("cleanup_pending", False))
            finally:
                for proc in processes:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait(timeout=5)

    def test_unconfirmed_launch_cleanup_retains_capacity_and_attention(self):
        from tests.support.repository_cycle import RepositoryCycleFixture

        fixture = RepositoryCycleFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        first = run_once.run_once(
            fixture.config_path, fixture_path=fixture.fixture_path, dry_run=True, skip_external=True, skip_publish=True
        )
        self.assertTrue(first["ok"], first)
        with patch.object(
            dispatch,
            "dispatch_worker",
            return_value={
                "ok": False,
                "status": "failed_dispatch",
                "pid": 987654,
                "cleanup_pending": True,
                "process_exited": True,
                "returncode": 2,
                "safe_error": "worker process group cleanup was not confirmed",
            },
        ):
            result = run_once.run_once(fixture.config_path, dry_run=False, skip_external=True, skip_publish=True)
        self.assertFalse(result["ok"])
        with closing(sqlite3.connect(fixture.data_dir / "dd.sqlite3")) as conn:
            status, task_id = conn.execute("SELECT status,task_id FROM attempts").fetchone()
            self.assertEqual(status, "stale")
            self.assertEqual(
                capacity.running_attempt_count_for_repos(
                    conn, [row[0] for row in conn.execute("SELECT repo_id FROM repos")]
                ),
                1,
            )
            self.assertEqual(execution_store.get_binding(conn, "task", task_id)["binding_status"], "attention")
            self.assertIn(conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], ("queued", "running"))
