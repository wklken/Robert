from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest import mock

from robert_agent.integrations import openclaw
from robert_agent.integrations.openclaw import write_plugin


class OpenClawTests(unittest.TestCase):
    def test_generated_plugin_uses_robert_cli_and_has_no_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "robert-openclaw"
            result = write_plugin(plugin_dir)
            self.assertTrue(result["ok"])
            source = (plugin_dir / "index.js").read_text(encoding="utf-8")
        self.assertIn('"robert"', source)
        self.assertIn('"status"', source)
        self.assertIn('"task"', source)
        self.assertNotIn("cron", source.lower())
        self.assertNotIn("run_once", source)

    def test_existing_plugin_dir_error_suggests_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "robert-openclaw"
            plugin_dir.mkdir()
            result = write_plugin(plugin_dir)

        self.assertFalse(result["ok"])
        self.assertIn("--force", result["safe_error"])
        self.assertIn("robert openclaw status", result["safe_error"])

    def test_preflight_reports_missing_openclaw_cli(self):
        with mock.patch(
            "robert_agent.integrations.openclaw._run",
            side_effect=FileNotFoundError("openclaw"),
        ):
            result = openclaw.preflight_openclaw()

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "missing")
        self.assertIn("OpenClaw CLI is not available", result["safe_error"])

    def test_post_install_verify_reports_missing_live_commands(self):
        commands = {
            "commands": [
                {"name": "dd-status"},
                {"name": "robert-status"},
            ]
        }
        completed = subprocess.CompletedProcess(
            args=["openclaw"],
            returncode=0,
            stdout=json.dumps(commands),
            stderr="",
        )
        with mock.patch(
            "robert_agent.integrations.openclaw._run",
            return_value=completed,
        ):
            result = openclaw.verify_gateway_commands()

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "missing_commands")
        self.assertEqual(
            result["missing_commands"],
            [
                "robert-task",
                "robert-run",
                "robert-artifact",
                "robert-inbox",
                "robert-respond",
                "robert-messaging-context",
            ],
        )
        self.assertEqual(
            result["command"],
            [
                "openclaw",
                "gateway",
                "call",
                "commands.list",
                "--json",
                "--timeout",
                "10000",
            ],
        )
        self.assertIn("restart", result["safe_error"].lower())

    def test_force_preserves_unowned_directories_and_extra_plugin_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "data"
            target.mkdir()
            keep = target / "user.txt"
            keep.write_text("valuable")
            self.assertFalse(write_plugin(target, force=True)["ok"])
            self.assertEqual(keep.read_text(), "valuable")
            plugin = Path(tmp) / "plugin"
            self.assertTrue(write_plugin(plugin)["ok"])
            (plugin / "user.txt").write_text("custom")
            self.assertTrue(write_plugin(plugin, force=True)["ok"])
            self.assertEqual((plugin / "user.txt").read_text(), "custom")
            link = Path(tmp) / "link"
            link.symlink_to(plugin, target_is_directory=True)
            self.assertFalse(write_plugin(link, force=True)["ok"])

    def test_force_rejects_generated_file_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp) / "plugin"
            write_plugin(plugin)
            outside = Path(tmp) / "outside"
            outside.write_text("keep")
            (plugin / "index.js").unlink()
            (plugin / "index.js").symlink_to(outside)
            self.assertFalse(write_plugin(plugin, force=True)["ok"])
            self.assertEqual(outside.read_text(), "keep")

    def test_force_replaces_hard_link_without_modifying_other_name(self):
        import os

        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp) / "plugin"
            write_plugin(plugin)
            unrelated = Path(tmp) / "unrelated.txt"
            unrelated.write_text("keep unrelated contents")
            (plugin / "index.js").unlink()
            os.link(unrelated, plugin / "index.js")
            self.assertTrue(write_plugin(plugin, force=True)["ok"])
            self.assertEqual(unrelated.read_text(), "keep unrelated contents")
            self.assertNotEqual((plugin / "index.js").stat().st_ino, unrelated.stat().st_ino)
