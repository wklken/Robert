import unittest
import sys
import tempfile
from pathlib import Path

from robert_agent.dispatch import _worker_environment
from robert_agent.dispatch import dispatch_worker
from robert_agent.validate_config import _normalize_workers
from robert_agent.worker_adapters import normalize_worker_agent_config


class WorkerEnvironmentTests(unittest.TestCase):
    def test_worker_environment_is_allowlisted(self):
        source = {
            "PATH": "/bin",
            "HOME": "/home/test",
            "SECRET_TOKEN": "hidden",
            "CUSTOM_VALUE": "allowed",
        }
        result = _worker_environment(
            allowed_names=["CUSTOM_VALUE"],
            environ=source,
        )
        self.assertEqual(result["PATH"], "/bin")
        self.assertEqual(result["HOME"], "/home/test")
        self.assertEqual(result["CUSTOM_VALUE"], "allowed")
        self.assertNotIn("SECRET_TOKEN", result)

    def test_worker_config_rejects_working_directory(self):
        with self.assertRaisesRegex(ValueError, "working_directory"):
            _normalize_workers(
                {
                    "workers": {
                        "default": {
                            "adapter": "codex",
                            "command": "codex",
                            "model": "default",
                            "effort": "default",
                            "working_directory": "/repo",
                        }
                    }
                }
            )

    def test_command_adapter_requires_command_sequence(self):
        with self.assertRaisesRegex(ValueError, "YAML sequence"):
            normalize_worker_agent_config(
                {
                    "worker_agent": "command",
                    "worker_command": "custom-worker --flag",
                }
            )

    def test_zero_exit_worker_is_not_failed_during_startup_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt_path = root / "prompt.md"
            prompt_path.write_text("task prompt", encoding="utf-8")
            worker = root / "worker.py"
            worker.write_text("raise SystemExit(0)\n", encoding="utf-8")

            result = dispatch_worker(
                task_id="task-1",
                attempt_id="attempt-1",
                prompt_path=prompt_path,
                worker_agent="command",
                worker_command=[sys.executable, str(worker)],
                dry_run=False,
                startup_probe_seconds=0.1,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "running")
        self.assertTrue(result["process_exited"])
        self.assertEqual(result["returncode"], 0)
