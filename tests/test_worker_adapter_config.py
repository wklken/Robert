import unittest

from robert_agent.dispatch import _worker_environment
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
