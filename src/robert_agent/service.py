import os
from pathlib import Path
import plistlib
import shlex
import subprocess


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


def _service_definition(platform_name, executable, config_path):
    home = Path.home()
    if platform_name == "linux":
        return (
            home / ".config" / "systemd" / "user" / "robert.service",
            render_systemd_unit(executable, config_path),
        )
    if platform_name == "darwin":
        return (
            home / "Library" / "LaunchAgents" / "dev.robert.agent.plist",
            render_launch_agent(executable, config_path),
        )
    raise ValueError(f"unsupported service platform: {platform_name}")


def install_service(
    platform_name,
    executable,
    config_path,
    dry_run=False,
):
    path, content = _service_definition(
        platform_name,
        Path(executable),
        Path(config_path),
    )
    result = {
        "ok": True,
        "status": "planned" if dry_run else "installed",
        "path": str(path),
        "content": content,
    }
    if dry_run:
        return result
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return result


def uninstall_service(platform_name, dry_run=False):
    path, _ = _service_definition(
        platform_name,
        Path("robert"),
        Path("~/.config/robert/config.yml"),
    )
    if dry_run:
        return {
            "ok": True,
            "status": "planned",
            "path": str(path),
        }
    path.unlink(missing_ok=True)
    return {
        "ok": True,
        "status": "uninstalled",
        "path": str(path),
    }


def _service_command(platform_name, action):
    if platform_name == "linux":
        return ["systemctl", "--user", action, "robert.service"]
    if platform_name == "darwin":
        domain = f"gui/{os.getuid()}"
        label = f"{domain}/dev.robert.agent"
        path, _ = _service_definition(
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


def control_service(platform_name, action, dry_run=False):
    if action not in {"start", "stop", "restart", "status"}:
        raise ValueError(f"unsupported service action: {action}")
    command = _service_command(platform_name, action)
    if dry_run:
        return {
            "ok": True,
            "status": "planned",
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
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "safe_error": completed.stderr.strip(),
    }
