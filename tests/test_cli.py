from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import yaml

from robert_agent import storage
from robert_agent.cli.main import main
from robert_agent.init_config import init_config


class CliTests(unittest.TestCase):
    def test_root_help_lists_public_commands(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        for command in [
            "init",
            "doctor",
            "status",
            "run",
            "task",
            "artifact",
            "config",
            "daemon",
            "service",
            "web",
            "migrate",
            "openclaw",
            "diagnostics",
        ]:
            self.assertIn(command, output.getvalue())

    def test_init_help_documents_non_interactive_values(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main(["init", "--help"])
        self.assertEqual(raised.exception.code, 0)
        for option in [
            "--non-interactive",
            "--repo",
            "--repo-path",
            "--worker",
            "--github-account",
            "--trusted-actor",
            "--force",
        ]:
            self.assertIn(option, output.getvalue())
        self.assertIn(
            "robert init --non-interactive",
            output.getvalue(),
        )

    def test_config_path_prints_environment_override(self):
        output = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {"ROBERT_CONFIG": "/tmp/robert.yml"},
            clear=True,
        ), redirect_stdout(output):
            code = main(["config", "path"])
        self.assertEqual(code, 0)
        self.assertIn("/tmp/robert.yml", output.getvalue())

    def test_status_output_mode_controls_rendering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            config_path = root / "config.yml"
            init_config(
                config_path,
                {
                    "repo": "example/backend",
                    "repo_path": str(repo),
                    "worker": "codex",
                    "github_account": "robert-bot",
                    "trusted_actor": "maintainer",
                },
                non_interactive=True,
            )
            config = yaml.safe_load(
                config_path.read_text(encoding="utf-8")
            )
            config["data_dir"] = str(root / "data")
            config_path.write_text(
                yaml.safe_dump(config, sort_keys=False),
                encoding="utf-8",
            )
            storage.init_database(root / "data" / "robert.sqlite3")

            text_output = io.StringIO()
            with redirect_stdout(text_output):
                text_code = main(
                    [
                        "status",
                        "--config",
                        str(config_path),
                        "--output",
                        "text",
                    ]
                )
            json_output = io.StringIO()
            with redirect_stdout(json_output):
                json_code = main(
                    [
                        "status",
                        "--config",
                        str(config_path),
                        "--output",
                        "json",
                    ]
                )

        self.assertEqual(text_code, 0)
        self.assertFalse(text_output.getvalue().lstrip().startswith("{"))
        self.assertEqual(json_code, 0)
        self.assertTrue(json.loads(json_output.getvalue())["ok"])
