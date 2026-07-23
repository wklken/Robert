from pathlib import Path
import subprocess
import unittest
from unittest import mock

from robert_agent import service
from robert_agent.service import (
    install_service,
    render_launch_agent,
    render_supervisord_config,
    render_systemd_unit,
)


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

    def test_supervisord_config_runs_foreground_daemon(self):
        text = render_supervisord_config(
            Path("/opt/robert/bin/robert"),
            Path("/home/test/.config/robert/config.yml"),
            Path("/home/test/.local/share/robert"),
        )
        self.assertIn("[program:robert-daemon]", text)
        self.assertIn(
            "command=/opt/robert/bin/robert daemon run "
            "--config /home/test/.config/robert/config.yml",
            text,
        )
        self.assertIn("autorestart=true", text)
        self.assertIn(
            "file=/home/test/.local/share/robert/run/supervisor.sock",
            text,
        )
        self.assertIn(
            "stdout_logfile=/home/test/.local/share/robert/logs/daemon.out.log",
            text,
        )
        self.assertNotIn("TOKEN", text)

    def test_linux_install_uses_supervisor_when_systemd_user_bus_unavailable(self):
        with mock.patch(
            "robert_agent.service._linux_systemd_user_available",
            return_value=False,
        ), mock.patch(
            "robert_agent.service.Path.home",
            return_value=Path("/home/test"),
        ):
            result = install_service(
                "linux",
                Path("/opt/robert/bin/robert"),
                Path("/home/test/.config/robert/config.yml"),
                dry_run=True,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["backend"], "supervisor")
        self.assertEqual(
            result["path"],
            "/home/test/.config/robert/supervisord.conf",
        )
        self.assertIn("[program:robert-daemon]", result["content"])

    def test_linux_install_keeps_systemd_when_user_bus_available(self):
        with mock.patch(
            "robert_agent.service._linux_systemd_user_available",
            return_value=True,
        ), mock.patch(
            "robert_agent.service.Path.home",
            return_value=Path("/home/test"),
        ):
            result = install_service(
                "linux",
                Path("/opt/robert/bin/robert"),
                Path("/home/test/.config/robert/config.yml"),
                dry_run=True,
            )
        self.assertEqual(result["backend"], "systemd")
        self.assertEqual(
            result["path"],
            "/home/test/.config/systemd/user/robert.service",
        )

    def test_supervisor_start_launches_supervisord_before_status(self):
        calls = []

        def fake_run(command, text, capture_output, check):
            calls.append(command)
            if "supervisor.supervisorctl" in command:
                if len(calls) == 1:
                    return subprocess.CompletedProcess(
                        command,
                        2,
                        "",
                        "no such file",
                    )
                return subprocess.CompletedProcess(
                    command,
                    0,
                    "robert-daemon RUNNING pid 42\n",
                    "",
                )
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch(
            "robert_agent.service._linux_systemd_user_available",
            return_value=False,
        ), mock.patch(
            "robert_agent.service.subprocess.run",
            side_effect=fake_run,
        ), mock.patch(
            "robert_agent.service.sys.executable",
            "/opt/robert/venv/bin/python",
        ), mock.patch(
            "robert_agent.service.Path.home",
            return_value=Path("/home/test"),
        ):
            result = service.control_service("linux", "start")

        self.assertTrue(result["ok"])
        self.assertEqual(result["backend"], "supervisor")
        self.assertEqual(result["programs"]["robert-daemon"], "RUNNING")
        self.assertEqual(calls[0][2], "supervisor.supervisorctl")
        self.assertEqual(calls[1][2], "supervisor.supervisord")
        self.assertEqual(calls[2][2], "supervisor.supervisorctl")
