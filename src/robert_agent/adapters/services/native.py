import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys

from robert_agent.paths import default_data_dir


SUPERVISOR_PROGRAM = "robert-daemon"
SUPERVISION_PROGRAM = "robert-task-supervisor"
_SERVICE_TARGETS = ("daemon", "supervisor")
_SYSTEMD_UNITS = {"daemon": "robert.service", "supervisor": "robert-supervisor.service"}
_LAUNCHD_LABELS = {"daemon": "dev.robert.agent", "supervisor": "dev.robert.supervisor"}


def _run_arguments(executable, config_path, target):
    arguments = [str(executable), target, "run", "--config", str(config_path)]
    if target == "daemon":
        arguments.append("--wait-for-lease")
    return arguments


def render_systemd_unit(executable: Path, config_path: Path, *, target="daemon") -> str:
    command = shlex.join(_run_arguments(executable, config_path, target))
    description = (
        "Robert — Your Repo Teammate"
        if target == "daemon"
        else "Robert Task Supervisor"
    )
    return (
        "[Unit]\n"
        f"Description={description}\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={command}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def render_launch_agent(executable: Path, config_path: Path, *, target="daemon") -> str:
    payload = {
        "Label": _LAUNCHD_LABELS[target],
        "ProgramArguments": _run_arguments(executable, config_path, target),
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
    *,
    target="daemon",
) -> str:
    data_dir = Path(data_dir or default_data_dir()).expanduser()
    run_dir = data_dir / "run"
    log_dir = data_dir / "logs"
    command = shlex.join(_run_arguments(executable, config_path, target))
    socket_name = "supervisor.sock" if target == "daemon" else "supervision.sock"
    manager_name = "supervisord" if target == "daemon" else "supervisord.supervision"
    header = (
        "[unix_http_server]\n"
        f"file={run_dir / socket_name}\n"
        "chmod=0700\n\n"
        "[supervisord]\n"
        "nodaemon=false\n"
        f"pidfile={run_dir / (manager_name + '.pid')}\n"
        f"logfile={log_dir / (manager_name + '.log')}\n"
        f"childlogdir={log_dir}\n\n"
        "[rpcinterface:supervisor]\n"
        "supervisor.rpcinterface_factory = "
        "supervisor.rpcinterface:make_main_rpcinterface\n\n"
        "[supervisorctl]\n"
        f"serverurl=unix://{run_dir / socket_name}\n\n"
    )
    if target == "supervisor":
        # A distinct manager lets supervision start without starting daemon/web.
        return header + (
            f"[program:{SUPERVISION_PROGRAM}]\n"
            f"command={command}\n"
            "autorestart=true\n"
            "startsecs=5\n"
            "stopsignal=TERM\n"
            "stopwaitsecs=30\n"
            f"stdout_logfile={log_dir / 'task-supervisor.out.log'}\n"
            f"stderr_logfile={log_dir / 'task-supervisor.err.log'}\n"
        )
    web_command = " ".join(
        shlex.quote(str(part))
        for part in [
            executable,
            "web",
            "run",
            "--config",
            config_path,
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
        ]
    )
    return header + (
        f"[program:{SUPERVISOR_PROGRAM}]\n"
        f"command={command}\n"
        "autorestart=true\n"
        "startsecs=5\n"
        "stopsignal=TERM\n"
        # TERM grace + KILL reap + process-group proof for the active child.
        "stopwaitsecs=30\n"
        f"stdout_logfile={log_dir / 'daemon.out.log'}\n"
        f"stderr_logfile={log_dir / 'daemon.err.log'}\n\n"
        "[program:robert-web]\n"
        f"command={web_command}\n"
        "autorestart=true\n"
        "startsecs=5\n"
        "stopsignal=TERM\n"
        f"stdout_logfile={log_dir / 'web.out.log'}\n"
        f"stderr_logfile={log_dir / 'web.err.log'}\n"
    )


def _selected_targets(target):
    if target == "all":
        return _SERVICE_TARGETS
    if target not in _SERVICE_TARGETS:
        raise ValueError(f"unsupported service target: {target}")
    return (target,)


def _service_backend(platform_name):
    if platform_name == "linux":
        return _linux_service_backend()
    if platform_name == "darwin":
        return "launchd"
    raise ValueError(f"unsupported service platform: {platform_name}")


def _service_path(backend, target):
    home = Path.home()
    if backend == "systemd":
        return home / ".config" / "systemd" / "user" / _SYSTEMD_UNITS[target]
    if backend == "launchd":
        return home / "Library" / "LaunchAgents" / (_LAUNCHD_LABELS[target] + ".plist")
    name = "supervisord.conf" if target == "daemon" else "supervisord.supervision.conf"
    return home / ".config" / "robert" / name


def _definition_result(backend, target, status, definitions):
    result = {
        **definitions[0],
        "ok": True,
        "status": status,
        "backend": backend,
        "target": target,
    }
    if len(definitions) > 1:
        result["definitions"] = definitions
    return result


def install_service(
    platform_name,
    executable,
    config_path,
    dry_run=False,
    *,
    target="all",
):
    targets = _selected_targets(target)
    backend = _service_backend(platform_name)
    render = {
        "systemd": render_systemd_unit,
        "launchd": render_launch_agent,
        "supervisor": render_supervisord_config,
    }[backend]
    executable = Path(executable).expanduser().absolute()
    config_path = Path(config_path).expanduser().absolute()
    definitions = [
        {
            "target": name,
            "path": str(_service_path(backend, name)),
            "content": render(executable, config_path, target=name),
        }
        for name in targets
    ]
    if not dry_run:
        if backend == "supervisor":
            data_dir = default_data_dir()
            (data_dir / "run").mkdir(parents=True, exist_ok=True)
            (data_dir / "logs").mkdir(parents=True, exist_ok=True)
        for definition in definitions:
            path = Path(definition["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(definition["content"], encoding="utf-8")
            temporary.replace(path)
    return _definition_result(
        backend, target, "planned" if dry_run else "installed", definitions
    )


def uninstall_service(platform_name, dry_run=False, *, target="all"):
    targets = _selected_targets(target)
    backend = _service_backend(platform_name)
    definitions = [
        {"target": name, "path": str(_service_path(backend, name))} for name in targets
    ]
    if not dry_run:
        for definition in definitions:
            Path(definition["path"]).unlink(missing_ok=True)
    return _definition_result(
        backend, target, "planned" if dry_run else "uninstalled", definitions
    )


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


def _native_command(backend, action, target):
    if backend == "systemd":
        return ["systemctl", "--user", action, _SYSTEMD_UNITS[target]]
    domain = f"gui/{os.getuid()}"
    label = f"{domain}/{_LAUNCHD_LABELS[target]}"
    commands = {
        "start": [
            "launchctl",
            "bootstrap",
            domain,
            str(_service_path(backend, target)),
        ],
        "stop": ["launchctl", "bootout", label],
        "restart": ["launchctl", "kickstart", "-k", label],
        "status": ["launchctl", "print", label],
    }
    return commands[action]


def _parse_supervisor_status(stdout):
    programs = {}
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] in {
            "STOPPED",
            "STARTING",
            "RUNNING",
            "BACKOFF",
            "STOPPING",
            "EXITED",
            "FATAL",
            "UNKNOWN",
        }:
            programs[parts[0]] = parts[1]
    return programs


def _run_command(command):
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )


def _command_result(backend, target, command, completed=None):
    if completed is None:
        return {
            "ok": True,
            "status": "planned",
            "backend": backend,
            "target": target,
            "command": command,
        }
    return {
        "ok": completed.returncode == 0,
        "status": "completed" if completed.returncode == 0 else "failed",
        "backend": backend,
        "target": target,
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "safe_error": completed.stderr.strip(),
    }


def _control_supervisor(action, target, whole_stack, dry_run):
    backend = "supervisor"
    config = str(_service_path(backend, target))
    program = SUPERVISOR_PROGRAM if target == "daemon" else SUPERVISION_PROGRAM
    ctl = [sys.executable, "-m", "supervisor.supervisorctl", "-c", config]
    bootstrap = [sys.executable, "-m", "supervisor.supervisord", "-c", config]
    status_command = ctl + ["status"] + ([] if whole_stack else [program])
    start_programs = ctl + ["start", "all" if whole_stack else program]
    commands = {
        "start": bootstrap if whole_stack else start_programs,
        "stop": ctl
        + (
            ["shutdown"] if whole_stack or target == "supervisor" else ["stop", program]
        ),
        "restart": ctl + ["restart", program],
        "status": status_command,
    }
    command = commands[action]
    if dry_run:
        plan = _command_result(backend, target, command)
        if action == "start":
            plan.update(
                status_command=status_command,
                bootstrap_command=bootstrap,
                start_programs_command=start_programs,
            )
        return plan

    if action == "start":
        before = _run_command(status_command)
        if before.returncode != 0:
            # `status` also fails when supervisord is up but programs are stopped.
            # Start them through the existing manager instead of opening its socket again.
            command = (
                start_programs if _parse_supervisor_status(before.stdout) else bootstrap
            )
            started = _run_command(command)
            if started.returncode != 0:
                return _command_result(backend, target, command, started)
        completed = _run_command(status_command)
    else:
        completed = _run_command(command)
    result = _command_result(backend, target, command, completed)
    result["programs"] = (
        _parse_supervisor_status(completed.stdout)
        if action in {"start", "status"}
        else {}
    )
    return result


def _control_target(backend, action, target, whole_stack, dry_run):
    try:
        if backend == "supervisor":
            return _control_supervisor(action, target, whole_stack, dry_run)
        command = _native_command(backend, action, target)
        return _command_result(
            backend, target, command, None if dry_run else _run_command(command)
        )
    except OSError as exc:
        return {
            "ok": False,
            "status": "failed",
            "backend": backend,
            "target": target,
            "safe_error": str(exc),
        }


def control_service(platform_name, action, dry_run=False, *, target="all"):
    if action not in {"start", "stop", "restart", "status"}:
        raise ValueError(f"unsupported service action: {action}")
    targets = _selected_targets(target)
    backend = _service_backend(platform_name)
    whole_stack = target == "all"
    if whole_stack:
        # Keep legacy behavior for installations without a supervision definition.
        targets = tuple(
            name for name in targets if _service_path(backend, name).is_file()
        ) or ("daemon",)
        if action == "stop":
            targets = targets[::-1]
    results = {
        name: _control_target(backend, action, name, whole_stack, dry_run)
        for name in targets
    }
    primary = results.get("daemon", results[targets[0]])
    if len(results) == 1:
        return {**primary, "target": target}
    succeeded = sum(result["ok"] for result in results.values())
    result = {
        **primary,
        "ok": succeeded == len(results),
        "status": "planned"
        if dry_run
        else (
            "completed"
            if succeeded == len(results)
            else "partial"
            if succeeded
            else "failed"
        ),
        "target": target,
        "commands": [
            entry["command"] for entry in results.values() if "command" in entry
        ],
        "results": results,
    }
    if not dry_run:
        failures = [entry for entry in results.values() if not entry["ok"]]
        result["returncode"] = failures[0].get("returncode", 1) if failures else 0
        result["safe_error"] = "\n".join(
            entry["safe_error"] for entry in failures if entry["safe_error"]
        )
        if backend == "supervisor":
            result["programs"] = {
                name: state
                for entry in results.values()
                for name, state in entry.get("programs", {}).items()
            }
    return result
