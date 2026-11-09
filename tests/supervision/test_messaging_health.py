"""The OpenClaw transport must still alert when the primary database is down."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from robert_agent.supervision import health
from robert_agent.supervision.application import checks


class MessagingHealthTests(unittest.TestCase):
    def test_outage_delivers_without_database_and_does_not_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "db_path": str(Path(tmp) / "missing.db"),
                "data_dir": tmp,
                "supervision": {"poll_seconds": 60},
                "messaging": {
                    "enabled": True,
                    "send_after": "2026-11-06T00:00:00Z",
                    "provider": "openclaw",
                    "channel": "telegram",
                    "account": "personal",
                    "target": "42",
                },
            }
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"ok":true}', ""))
            with patch.dict("os.environ", {}, clear=True):
                checks.check(config, record=True, now="2026-11-07T00:00:00Z")
                checks.deliver_health(config["db_path"], health.health_path(config), config=config, runner=runner)
                self.assertEqual(runner.call_count, 0)
                checks.check(config, record=True, now="2026-11-07T00:01:00Z")
                checks.deliver_health(config["db_path"], health.health_path(config), config=config, runner=runner)
                checks.deliver_health(config["db_path"], health.health_path(config), config=config, runner=runner)
            self.assertEqual(runner.call_count, 1)
            argv = runner.call_args.args[0]
            self.assertEqual(argv[:3], ["openclaw", "message", "send"])
            self.assertIn("监督服务无法读取数据库", argv[argv.index("--message") + 1])
            self.assertFalse(Path(config["db_path"]).exists())
            state = json.loads(health.health_path(config).with_name("health-delivery.json").read_text())
            self.assertEqual(state["status"], "sent")

    def test_outage_started_before_activation_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "db_path": str(Path(tmp) / "missing.db"),
                "data_dir": tmp,
                "supervision": {"poll_seconds": 60},
                "messaging": {
                    "enabled": True,
                    "provider": "openclaw",
                    "channel": "wecom",
                    "account": "default",
                    "target": "operator",
                    "send_after": "2026-11-09T00:00:00Z",
                },
            }
            runner = Mock(side_effect=AssertionError("Historical outage must not send"))
            with patch.dict("os.environ", {}, clear=True):
                checks.check(config, record=True, now="2026-11-07T00:00:00Z")
                checks.check(config, record=True, now="2026-11-07T00:01:00Z")
                checks.deliver_health(config["db_path"], health.health_path(config), config=config, runner=runner)
            runner.assert_not_called()
