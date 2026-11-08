"""Prepare a credentials-free installation and manage real Robert services."""

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import sysconfig

from client import fixture


STATE = Path("/state")
GIT = Path("/git")
HARNESS = Path(__file__).resolve().parent
CONFIG = STATE / "config.json"
DB = STATE / "data" / "robert.sqlite3"
SUPERVISOR = STATE / "supervisord.conf"
ACCOUNT = "robot"
REPO = "example/repo"
OTHER = "example/other"


def run(*argv, cwd=None):
    return subprocess.check_output(argv, cwd=cwd, text=True, stderr=subprocess.PIPE, timeout=30).strip()


def git_seed(repo):
    checkout = STATE / "checkouts" / repo
    checkout.mkdir(parents=True)
    bare = GIT / f"{repo}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    run("git", "init", "--bare", "--initial-branch=main", str(bare))
    run("git", "init", "--initial-branch=main", cwd=checkout)
    (checkout / "calc.py").write_text("def add(x, y):\n    return x - y\n")
    run("git", "add", "calc.py", cwd=checkout)
    run("git", "commit", "-m", "Seed fixture", cwd=checkout)
    run("git", "push", str(bare), "main", cwd=checkout)
    for remote in ("origin", "upstream"):
        run("git", "remote", "add", remote, f"git@github.com:{repo}.git", cwd=checkout)
    hook = bare / "hooks" / "post-receive"
    hook.write_text(
        "#!/bin/sh\nexec "
        + shlex.join([sys.executable, str(HARNESS / "git_transport.py"), "post-receive", repo])
        + "\n"
    )
    hook.chmod(0o755)
    return {
        "full_name": repo,
        "trusted_actors": ["maintainer"],
        "default_branch": "main",
        "checkout": str(checkout),
        "worktrees": str(STATE / "worktrees" / repo),
    }


def configuration(repos):
    return {
        "version": 2,
        "data_dir": str(DB.parent),
        "database": DB.name,
        "default_worker": "fixture",
        "github": {"account": ACCOUNT, "polling": {"fallback_seconds": 2}},
        "scheduler": {
            "max_concurrency": 1,
            "model_max_concurrency": 2,
            "local_poll_seconds": 1,
            "lease_ttl_seconds": 300,
        },
        "daemon": {"enabled": True, "control_timeouts": {"github_scan_seconds": 60, "local_cycle_seconds": 30}},
        "maintenance": {"enabled": False},
        "supervision": {
            "mode": "observe",
            "poll_seconds": 1,
            "stall_seconds": 8,
            "startup_grace_seconds": 30,
            "heartbeat_stale_seconds": 300,
            "hard_timeout_seconds": 900,
        },
        "workers": {
            "fixture": {
                "adapter": "command",
                "model": "default",
                "effort": "default",
                "command": [sys.executable, str(HARNESS / "fake_worker.py"), str(STATE)],
                "timeout_seconds": 900,
            }
        },
        "repos": repos,
    }


def write_json(path, value):
    scratch = path.with_suffix(".next")
    scratch.write_text(json.dumps(value, indent=2) + "\n")
    scratch.replace(path)


def prepare(case_id):
    import robert_agent

    installed = Path(robert_agent.__file__).resolve()
    if not installed.is_relative_to(Path(sysconfig.get_path("purelib"))) or os.environ.get("PYTHONPATH"):
        raise RuntimeError(f"expected a clean installed wheel, got {installed}")
    info = fixture("/health")
    run("git", "config", "--global", "user.name", "Robert fixture")
    run("git", "config", "--global", "user.email", "robot@example.invalid")
    run("git", "config", "--global", "core.sshCommand", shlex.join([sys.executable, str(HARNESS / "git_transport.py")]))
    run("git", "config", "--global", "ssh.variant", "simple")
    config = configuration([git_seed(repo) for repo in (REPO, OTHER)])
    write_json(CONFIG, config)
    write_json(
        STATE / "installation.json",
        {
            "case_id": case_id,
            "module": str(installed),
            "python": sys.version,
            "fixture": info,
            "wheel_sha256": Path("/usr/local/share/robert-wheel.sha256").read_text().split()[0],
        },
    )
    return config


def serve():
    log_dir = STATE / "logs"
    log_dir.mkdir(exist_ok=True)
    socket = STATE / "supervisor.sock"
    base = f"""[unix_http_server]
file={socket}
chmod=0600
[supervisord]
nodaemon=true
logfile={log_dir / "supervisord.log"}
pidfile={STATE / "supervisord.pid"}
[supervisorctl]
serverurl=unix://{socket}
[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface
"""
    for priority, (role, argv) in enumerate(
        [
            ("observer", ["supervisor", "run"]),
            ("observer-notify", ["supervisor", "run"]),
            ("daemon-dry", ["daemon", "run", "--dry-run"]),
            ("daemon", ["daemon", "run"]),
            ("web", ["web", "run", "--writable"]),
            ("readonly", ["web", "run"]),
        ],
        start=1,
    ):
        base += f"""[program:{role}]
command={shlex.join(["robert", *argv, "--config", str(CONFIG)])}
directory=/state
priority={priority}
autostart=false
autorestart=false
startsecs=0
stopwaitsecs=7
redirect_stderr=true
stdout_logfile={log_dir / (role + ".log")}
"""
        if role == "observer-notify":
            base += f'environment=ROBERT_NOTIFICATION_COMMAND="{sys.executable} /harness/notification_sink.py"\n'
    SUPERVISOR.write_text(base)
    os.execvp("supervisord", ["supervisord", "-c", str(SUPERVISOR)])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve"])
    parser.parse_args()
    serve()
