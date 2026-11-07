import configparser
from pathlib import Path
import plistlib
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock

from robert_agent.adapters.services import native as service
from robert_agent.adapters.services.native import (
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
            "ExecStart=/opt/robert/bin/robert daemon run --config /home/test/.config/robert/config.yml",
            text,
        )
        self.assertIn("Restart=on-failure", text)
        self.assertIn("--wait-for-lease", text)
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
        self.assertIn("<string>--wait-for-lease</string>", text)

    def test_supervisord_config_runs_foreground_daemon(self):
        text = render_supervisord_config(
            Path("/opt/robert/bin/robert"),
            Path("/home/test/.config/robert/config.yml"),
            Path("/home/test/.local/share/robert"),
        )
        self.assertIn("[program:robert-daemon]", text)
        self.assertIn(
            "command=/opt/robert/bin/robert daemon run --config /home/test/.config/robert/config.yml",
            text,
        )
        self.assertIn("autorestart=true", text)
        daemon_section = text.split("[program:robert-daemon]", 1)[1].split("[program:", 1)[0]
        self.assertIn("--wait-for-lease", daemon_section)
        # Room for TERM grace, KILL reap, and process-group proof of the active child.
        self.assertIn("stopwaitsecs=30", daemon_section)
        self.assertIn(
            "file=/home/test/.local/share/robert/run/supervisor.sock",
            text,
        )
        self.assertIn(
            "stdout_logfile=/home/test/.local/share/robert/logs/daemon.out.log",
            text,
        )
        self.assertNotIn("TOKEN", text)

    def test_supervisord_config_runs_local_read_only_web_ui(self):
        text = render_supervisord_config(
            Path("/opt/robert/bin/robert"),
            Path("/home/test/.config/robert/config.yml"),
            Path("/home/test/.local/share/robert"),
        )
        self.assertIn("[program:robert-web]", text)
        self.assertIn(
            "command=/opt/robert/bin/robert web run "
            "--config /home/test/.config/robert/config.yml "
            "--host 127.0.0.1 --port 8765",
            text,
        )
        self.assertIn(
            "stdout_logfile=/home/test/.local/share/robert/logs/web.out.log",
            text,
        )
        self.assertIn(
            "stderr_logfile=/home/test/.local/share/robert/logs/web.err.log",
            text,
        )
        self.assertEqual(text.count("autorestart=true"), 2)
        self.assertNotIn("--writable", text)
        self.assertNotIn("--allow-remote", text)

    def test_linux_install_uses_supervisor_when_systemd_user_bus_unavailable(self):
        with (
            mock.patch(
                "robert_agent.adapters.services.native._linux_systemd_user_available",
                return_value=False,
            ),
            mock.patch(
                "robert_agent.adapters.services.native.Path.home",
                return_value=Path("/home/test"),
            ),
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
        self.assertIn("[program:robert-web]", result["content"])

    def test_linux_install_keeps_systemd_when_user_bus_available(self):
        with (
            mock.patch(
                "robert_agent.adapters.services.native._linux_systemd_user_available",
                return_value=True,
            ),
            mock.patch(
                "robert_agent.adapters.services.native.Path.home",
                return_value=Path("/home/test"),
            ),
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

        with (
            mock.patch(
                "robert_agent.adapters.services.native._linux_systemd_user_available",
                return_value=False,
            ),
            mock.patch(
                "robert_agent.adapters.services.native.subprocess.run",
                side_effect=fake_run,
            ),
            mock.patch(
                "robert_agent.adapters.services.native.sys.executable",
                "/opt/robert/venv/bin/python",
            ),
            mock.patch(
                "robert_agent.adapters.services.native.Path.home",
                return_value=Path("/home/test"),
            ),
        ):
            result = service.control_service("linux", "start")

        self.assertTrue(result["ok"])
        self.assertEqual(result["backend"], "supervisor")
        self.assertEqual(result["programs"]["robert-daemon"], "RUNNING")
        self.assertEqual(calls[0][2], "supervisor.supervisorctl")
        self.assertEqual(calls[1][2], "supervisor.supervisord")
        self.assertEqual(calls[2][2], "supervisor.supervisorctl")

    def test_supervisor_install_uninstall_preserves_daemon_definition(self):
        for platform, systemd in [("linux", True), ("linux", False), ("darwin", False)]:
            with (
                self.subTest(platform=platform, systemd=systemd),
                tempfile.TemporaryDirectory() as root,
            ):
                home = Path(root)
                config_path = home / "config with spaces.yml"
                executable = home / "bin" / "robert"
                with (
                    mock.patch.object(service.Path, "home", return_value=home),
                    mock.patch.object(
                        service,
                        "default_data_dir",
                        return_value=home / "data",
                    ),
                    mock.patch.object(service, "_linux_systemd_user_available", return_value=systemd),
                ):
                    daemon = install_service(platform, executable, config_path, target="daemon")
                    daemon_path = Path(daemon["path"])
                    daemon_content = daemon_path.read_text(encoding="utf-8")
                    preview = install_service(
                        platform,
                        executable,
                        config_path,
                        dry_run=True,
                        target="supervisor",
                    )
                    self.assertFalse(Path(preview["path"]).exists())
                    installed = install_service(platform, executable, config_path, target="supervisor")
                    path = Path(installed["path"])
                    content = path.read_text(encoding="utf-8")
                    expected_command = [
                        str(executable),
                        "supervisor",
                        "run",
                        "--config",
                        str(config_path),
                    ]
                    if systemd:
                        self.assertEqual(path.name, "robert-supervisor.service")
                        command = next(
                            line.split("=", 1)[1] for line in content.splitlines() if line.startswith("ExecStart=")
                        )
                        self.assertEqual(shlex.split(command), expected_command)
                    elif platform == "darwin":
                        definition = plistlib.loads(content.encode("utf-8"))
                        self.assertEqual(definition["Label"], "dev.robert.supervisor")
                        self.assertEqual(definition["ProgramArguments"], expected_command)
                    else:
                        definition = configparser.ConfigParser(interpolation=None)
                        definition.read_string(content)
                        self.assertNotIn("program:robert-daemon", definition)
                        self.assertNotIn("program:robert-web", definition)
                        self.assertEqual(
                            shlex.split(definition["program:robert-task-supervisor"]["command"]),
                            expected_command,
                        )
                        legacy_definition = configparser.ConfigParser(interpolation=None)
                        legacy_definition.read_string(daemon_content)
                        for section, option in [
                            ("unix_http_server", "file"),
                            ("supervisord", "pidfile"),
                            ("supervisord", "logfile"),
                        ]:
                            self.assertNotEqual(
                                definition[section][option],
                                legacy_definition[section][option],
                            )
                    service.uninstall_service(platform, target="supervisor", dry_run=True)
                    self.assertTrue(path.exists())
                    service.uninstall_service(platform, target="supervisor")
                    self.assertFalse(path.exists())
                    self.assertEqual(daemon_path.read_text(encoding="utf-8"), daemon_content)

    def test_daemon_only_stop_and_all_stop_target_correct_services(self):
        for platform, systemd in [("linux", True), ("linux", False), ("darwin", False)]:
            with (
                self.subTest(platform=platform, systemd=systemd),
                tempfile.TemporaryDirectory() as root,
            ):
                home = Path(root)
                with (
                    mock.patch.object(service.Path, "home", return_value=home),
                    mock.patch.object(
                        service,
                        "default_data_dir",
                        return_value=home / "data",
                    ),
                    mock.patch.object(service, "_linux_systemd_user_available", return_value=systemd),
                ):
                    install_service(platform, home / "bin" / "robert", home / "config.yml")
                    calls = []

                    def run(command, **kwargs):
                        calls.append(command)
                        return subprocess.CompletedProcess(command, 0, "", "")

                    with mock.patch.object(service.subprocess, "run", side_effect=run):
                        daemon = service.control_service(platform, "stop", target="daemon")
                        self.assertTrue(daemon["ok"])
                        self.assertEqual(calls, [daemon["command"]])
                        if systemd:
                            self.assertEqual(
                                calls[0],
                                ["systemctl", "--user", "stop", "robert.service"],
                            )
                        elif platform == "darwin":
                            self.assertTrue(calls[0][-1].endswith("/dev.robert.agent"))
                        else:
                            self.assertEqual(calls[0][-2:], ["stop", "robert-daemon"])
                        calls.clear()
                        full = service.control_service(platform, "stop")
                        self.assertTrue(full["ok"])
                        self.assertEqual(len(calls), 2)
                        if systemd:
                            self.assertEqual(
                                {call[-1] for call in calls},
                                {"robert.service", "robert-supervisor.service"},
                            )
                        elif platform == "darwin":
                            self.assertEqual(
                                {call[-1].rsplit("/", 1)[1] for call in calls},
                                {"dev.robert.agent", "dev.robert.supervisor"},
                            )
                        else:
                            self.assertEqual([call[-1] for call in calls], ["shutdown", "shutdown"])
                            self.assertNotEqual(calls[0][-2], calls[1][-2])

    def test_all_status_reports_partial_failure_after_checking_both_targets(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            with (
                mock.patch.object(service.Path, "home", return_value=home),
                mock.patch.object(
                    service,
                    "_linux_systemd_user_available",
                    return_value=True,
                ),
            ):
                install_service("linux", home / "bin" / "robert", home / "config.yml")

                def run(command, **kwargs):
                    active = command[-1] == "robert-supervisor.service"
                    return subprocess.CompletedProcess(
                        command,
                        0 if active else 3,
                        "active" if active else "inactive",
                        "",
                    )

                with mock.patch.object(service.subprocess, "run", side_effect=run):
                    status = service.control_service("linux", "status")
            self.assertFalse(status["ok"])
            self.assertEqual(status["status"], "partial")
            self.assertFalse(status["results"]["daemon"]["ok"])
            self.assertTrue(status["results"]["supervisor"]["ok"])

    def test_supervisor_start_does_not_start_daemon_manager(self):
        calls = []
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            with (
                mock.patch.object(service.Path, "home", return_value=home),
                mock.patch.object(
                    service,
                    "default_data_dir",
                    return_value=home / "data",
                ),
                mock.patch.object(service, "_linux_systemd_user_available", return_value=False),
            ):
                installed = install_service(
                    "linux",
                    home / "bin" / "robert",
                    home / "config.yml",
                    target="supervisor",
                )

                def run(command, **kwargs):
                    calls.append(command)
                    if len(calls) == 1:
                        return subprocess.CompletedProcess(command, 2, "unix socket unavailable", "")
                    if "supervisor.supervisord" in command:
                        return subprocess.CompletedProcess(command, 0, "", "")
                    return subprocess.CompletedProcess(command, 0, "robert-task-supervisor RUNNING pid 42\n", "")

                with mock.patch.object(service.subprocess, "run", side_effect=run):
                    result = service.control_service("linux", "start", target="supervisor")
            self.assertTrue(result["ok"])
            self.assertEqual(result["programs"], {"robert-task-supervisor": "RUNNING"})
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(installed["path"] in call for call in calls))
            self.assertIn("supervisor.supervisord", calls[1])

    def test_start_after_targeted_stop_reuses_existing_daemon_manager(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if len(calls) == 1:
                return subprocess.CompletedProcess(command, 3, "robert-daemon STOPPED not started\n", "")
            if command[-2] == "start":
                return subprocess.CompletedProcess(command, 0, "robert-daemon: started\n", "")
            return subprocess.CompletedProcess(command, 0, "robert-daemon RUNNING pid 42\n", "")

        with (
            mock.patch.object(service, "_linux_systemd_user_available", return_value=False),
            mock.patch.object(
                service.Path,
                "home",
                return_value=Path("/home/test"),
            ),
            mock.patch.object(service.subprocess, "run", side_effect=run),
        ):
            result = service.control_service("linux", "start", target="daemon")

        self.assertTrue(result["ok"])
        self.assertEqual(
            [command[-2:] for command in calls],
            [
                ["status", "robert-daemon"],
                ["start", "robert-daemon"],
                ["status", "robert-daemon"],
            ],
        )
        self.assertTrue(all("supervisor.supervisorctl" in command for command in calls))
