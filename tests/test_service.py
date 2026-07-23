from pathlib import Path
import unittest

from robert_agent.service import render_launch_agent, render_systemd_unit


class ServiceTests(unittest.TestCase):
    def test_systemd_unit_runs_foreground_daemon(self):
        text = render_systemd_unit(
            Path("/opt/robert/bin/robert"),
            Path("/home/test/.config/robert/config.yml"),
        )
        self.assertIn(
            "ExecStart=/opt/robert/bin/robert daemon run "
            "--config /home/test/.config/robert/config.yml",
            text,
        )
        self.assertIn("Restart=on-failure", text)
        self.assertNotIn("TOKEN", text)

    def test_launch_agent_runs_foreground_daemon(self):
        text = render_launch_agent(
            Path("/opt/robert/bin/robert"),
            Path("/Users/test/.config/robert/config.yml"),
        )
        self.assertIn("<string>/opt/robert/bin/robert</string>", text)
        self.assertIn("<string>daemon</string>", text)
        self.assertIn("<string>run</string>", text)
        self.assertIn("<key>KeepAlive</key>", text)
