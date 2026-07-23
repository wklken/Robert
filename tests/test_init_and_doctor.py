from pathlib import Path
import tempfile
import unittest

from robert_agent.doctor import doctor
from robert_agent.init_config import init_config


class InitAndDoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()

    def test_non_interactive_init_writes_valid_config(self):
        config = self.root / "config.yml"
        result = init_config(
            config,
            {
                "repo": "example/backend",
                "repo_path": str(self.repo),
                "worker": "codex",
                "github_account": "robert-bot",
                "trusted_actor": "maintainer",
            },
            non_interactive=True,
        )
        self.assertTrue(result["ok"])
        self.assertIn("account: robert-bot", config.read_text())

    def test_doctor_reports_missing_worker_command(self):
        config = self.root / "config.yml"
        init_config(
            config,
            {
                "repo": "example/backend",
                "repo_path": str(self.repo),
                "worker": "missing-robert-worker",
                "github_account": "robert-bot",
                "trusted_actor": "maintainer",
            },
            non_interactive=True,
        )
        result = doctor(config, skip_external=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["checks"]["worker"]["status"], "failed")

    def test_config_rejects_github_token_fields(self):
        config = self.root / "config.yml"
        config.write_text(
            "version: 1\n"
            "github:\n"
            "  account: robert-bot\n"
            "  token: must-not-be-stored\n"
            "workers: {}\n"
            "repos: []\n",
            encoding="utf-8",
        )
        result = doctor(config, skip_external=True)
        self.assertFalse(result["ok"])
        self.assertIn("github.token", result["safe_error"])
