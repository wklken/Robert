from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest import mock

import yaml

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
        self.assertIn("workflow_engine:\n  mode: legacy", config.read_text())

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

    def test_doctor_skips_dbos_connectivity_in_legacy_mode(self):
        from robert_agent import dbos_runtime

        config = self.root / "config.yml"
        init_config(
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
        with mock.patch.object(dbos_runtime, "check_connectivity") as check:
            result = doctor(config, skip_external=True)

        check.assert_not_called()
        self.assertEqual(result["checks"]["workflow_engine"]["status"], "passed")
        self.assertTrue(result["checks"]["workflow_engine"]["skipped"])

    def test_doctor_dbos_failure_does_not_echo_database_url(self):
        config = self.root / "config.yml"
        init_config(
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
        config_values = yaml.safe_load(config.read_text(encoding="utf-8"))
        config_values["workflow_engine"] = {
            "mode": "shadow",
            "system_database_url_env": "ROBERT_DBOS_DOCTOR_URL",
        }
        config.write_text(
            yaml.safe_dump(config_values, sort_keys=False),
            encoding="utf-8",
        )
        secret_url = "postgresql://operator:secret@example.invalid/robert"
        with mock.patch.dict(
            os.environ,
            {"ROBERT_DBOS_DOCTOR_URL": secret_url},
            clear=False,
        ):
            result = doctor(config, skip_external=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["checks"]["workflow_engine"]["status"], "failed")
        self.assertNotIn(secret_url, json.dumps(result, sort_keys=True))
