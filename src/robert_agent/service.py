import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys

from robert_agent.paths import default_data_dir


SUPERVISOR_PROGRAM = "robert-daemon"


def render_systemd_unit(executable: Path, config_path: Path) -> str:
    command = " ".join(
        shlex.quote(str(part))
        for part in [
            executable,
            "daemon",
            "run",
            "--config",
            config_path,
        ]
    )
    return (
        "[Unit]\n"
        "Description=Robert — Your Repo Teammate\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={command}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def render_launch_agent(executable: Path, config_path: Path) -> str:
    payload = {
        "Label": "dev.robert.agent",
        "ProgramArguments": [
            str(executable),
            "daemon",
            "run",
            "--config",
            str(config_path),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
    }
    return plistlib.dumps(
        payload,
        fmt=plistlib.FMT_XML,
    ).decode("utf-8")


def render_supervisord_config(
    executable: Path,
    config_path: Path,
    data_dir: Path | None = None,
) -> str:
    data_dir = Path(data_dir or default_data_dir()).expanduser()
    run_dir = data_dir / "run"
    log_dir = data_dir / "logs"
    command = " ".join(
        shlex.quote(str(part))
        for part in [
            executable,
            "daemon",
            "run",
            "--config",
            config_path,
        ]
    )
    return (
        "[unix_http_server]\n"
        f"file={run_dir / 'supervisor.sock'}\n"
        "chmod=0700\n\n"
        "[supervisord]\n"
        "nodaemon=false\n"
        f"pidfile={run_dir / 'supervisord.pid'}\n"
        f"logfile={log_dir / 'supervisord.log'}\n"
        f"childlogdir={log_dir}\n\n"
        "[rpcinterface:supervisor]\n"
        "supervisor.rpcinterface_factory = "
        "supervisor.rpcinterface:make_main_rpcinterface\n\n"
        "[supervisorctl]\n"
        f"serverurl=unix://{run_dir / 'supervisor.sock'}\n\n"
        f"[program:{SUPERVISOR_PROGRAM}]\n"
        f"command={command}\n"
        "autorestart=true\n"
        "startsecs=5\n"
        "stopsignal=TERM\n"
        f"stdout_logfile={log_dir / 'daemon.out.log'}\n"
        f"stderr_logfile={log_dir / 'daemon.err.log'}\n"
    )


def _service_definition(platform_name, executable, config_path):
    home = Path.home()
    if platform_name == "linux":
        if _linux_service_backend() == "supervisor":
            return (
                home / ".config" / "robert" / "supervisord.conf",
                render_supervisord_config(executable, config_path),
                "supervisor",
            )
        return (
            home / ".config" / "systemd" / "user" / "robert.service",
            render_systemd_unit(executable, config_path),
            "systemd",
        )
    if platform_name == "darwin":
        return (
            home / "Library" / "LaunchAgents" / "dev.robert.agent.plist",
            render_launch_agent(executable, config_path),
            "launchd",
        )
    raise ValueError(f"unsupported service platform: {platform_name}")


def install_service(
    platform_name,
    executable,
    config_path,
    dry_run=False,
):
    path, content, backend = _service_definition(
        platform_name,
        Path(executable),
        Path(config_path),
    )
    result = {
        "ok": True,
        "status": "planned" if dry_run else "installed",
        "backend": backend,
        "path": str(path),
        "content": content,
    }
    if dry_run:
        return result
    path.parent.mkdir(parents=True, exist_ok=True)
    if backend == "supervisor":
        data_dir = default_data_dir()
        (data_dir / "run").mkdir(parents=True, exist_ok=True)
        (data_dir / "logs").mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return result


def uninstall_service(platform_name, dry_run=False):
    path, _, backend = _service_definition(
        platform_name,
        Path("robert"),
        Path("~/.config/robert/config.yml"),
    )
    if dry_run:
        return {
            "ok": True,
            "status": "planned",
            "backend": backend,
            "path": str(path),
        }
    path.unlink(missing_ok=True)
    return {
        "ok": True,
        "status": "uninstalled",
        "backend": backend,
        "path": str(path),
    }


def _linux_systemd_user_available():
    if not (Path("/run") / "systemd" / "system").exists():
        return False
    try:
        completed = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return completed.returncode == 0


def _linux_service_backend():
    return "systemd" if _linux_systemd_user_available() else "supervisor"


def _service_command(platform_name, action):
    if platform_name == "linux":
        if _linux_service_backend() == "supervisor":
            return _supervisor_command(action)
        return ["systemctl", "--user", action, "robert.service"]
    if platform_name == "darwin":
        domain = f"gui/{os.getuid()}"
        label = f"{domain}/dev.robert.agent"
        path, _, _ = _service_definition(
            platform_name,
            Path("robert"),
            Path("~/.config/robert/config.yml"),
        )
        commands = {
            "start": ["launchctl", "bootstrap", domain, str(path)],
            "stop": ["launchctl", "bootout", label],
            "restart": ["launchctl", "kickstart", "-k", label],
            "status": ["launchctl", "print", label],
        }
        return commands[action]
    raise ValueError(f"unsupported service platform: {platform_name}")


def _supervisor_config_path():
    return Path.home() / ".config" / "robert" / "supervisord.conf"


def _supervisor_command(action):
    config_path = _supervisor_config_path()
    if action == "start":
        return [
            sys.executable,
            "-m",
            "supervisor.supervisord",
            "-c",
            str(config_path),
        ]
    if action == "stop":
        return [
            sys.executable,
            "-m",
            "supervisor.supervisorctl",
            "-c",
            str(config_path),
            "shutdown",
        ]
    if action == "restart":
        return [
            sys.executable,
            "-m",
            "supervisor.supervisorctl",
            "-c",
            str(config_path),
            "restart",
            SUPERVISOR_PROGRAM,
        ]
    if action == "status":
        return [
            sys.executable,
            "-m",
            "supervisor.supervisorctl",
            "-c",
            str(config_path),
            "status",
        ]
    raise ValueError(f"unsupported service action: {action}")


def _parse_supervisor_status(stdout):
    programs = {}
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            programs[parts[0]] = parts[1]
    return programs


def _run_command(command):
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )


def _control_supervisor(action, dry_run=False):
    command = _supervisor_command(action)
    if dry_run:
        return {
            "ok": True,
            "status": "planned",
            "backend": "supervisor",
            "command": command,
        }

    if action == "start":
        status_command = _supervisor_command("status")
        status_before = _run_command(status_command)
        if status_before.returncode != 0:
            start_result = _run_command(command)
            if start_result.returncode != 0:
                return {
                    "ok": False,
                    "status": "failed",
                    "backend": "supervisor",
                    "command": command,
                    "returncode": start_result.returncode,
                    "stdout": start_result.stdout.strip(),
                    "safe_error": start_result.stderr.strip(),
                }
        status_result = _run_command(status_command)
        programs = _parse_supervisor_status(status_result.stdout)
        return {
            "ok": status_result.returncode == 0,
            "status": (
                "completed"
                if status_result.returncode == 0
                else "failed"
            ),
            "backend": "supervisor",
            "command": command,
            "returncode": status_result.returncode,
            "stdout": status_result.stdout.strip(),
            "safe_error": status_result.stderr.strip(),
            "programs": programs,
        }

    completed = _run_command(command)
    programs = (
        _parse_supervisor_status(completed.stdout)
        if action == "status"
        else {}
    )
    return {
        "ok": completed.returncode == 0,
        "status": (
            "completed"
            if completed.returncode == 0
            else "failed"
        ),
        "backend": "supervisor",
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "safe_error": completed.stderr.strip(),
        "programs": programs,
    }


def control_service(platform_name, action, dry_run=False):
    if action not in {"start", "stop", "restart", "status"}:
        raise ValueError(f"unsupported service action: {action}")
    if platform_name == "linux" and _linux_service_backend() == "supervisor":
        return _control_supervisor(action, dry_run=dry_run)
    command = _service_command(platform_name, action)
    if dry_run:
        return {
            "ok": True,
            "status": "planned",
            "backend": (
                "systemd"
                if platform_name == "linux"
                else "launchd"
            ),
            "command": command,
        }
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    return {
        "ok": completed.returncode == 0,
        "status": (
            "completed"
            if completed.returncode == 0
            else "failed"
        ),
        "backend": (
            "systemd"
            if platform_name == "linux"
            else "launchd"
        ),
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "safe_error": completed.stderr.strip(),
    }
