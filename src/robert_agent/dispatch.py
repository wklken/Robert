#!/usr/bin/env python3
import argparse
from contextlib import ExitStack
import os
import subprocess
import time
from pathlib import Path

from robert_agent import worker_adapters
from robert_agent.common import emit


_BACKGROUND_PROCESSES = {}
DEFAULT_WORKER_BASH_DEFAULT_TIMEOUT_MS = "300000"
DEFAULT_WORKER_ENV = {
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "SHELL",
    "USER",
}


def _reap_background_processes():
    for pid, proc in list(_BACKGROUND_PROCESSES.items()):
        if proc.poll() is not None:
            _BACKGROUND_PROCESSES.pop(pid, None)


def _worker_environment(allowed_names=(), environ=None):
    source = dict(os.environ if environ is None else environ)
    names = DEFAULT_WORKER_ENV | set(allowed_names)
    result = {
        name: source[name]
        for name in names
        if name in source
    }
    result.setdefault(
        "BASH_DEFAULT_TIMEOUT_MS",
        DEFAULT_WORKER_BASH_DEFAULT_TIMEOUT_MS,
    )
    return result


def build_worker_command(
    prompt_path,
    worktree_path=None,
    model="gpt-5.4",
    reasoning_effort="high",
    worker_command="cbc",
    worker_agent="cbc",
    worker_agent_config=None,
):
    _reap_background_processes()
    launch = worker_adapters.build_worker_launch(
        prompt_path=prompt_path,
        worktree_path=worktree_path,
        model=model,
        reasoning_effort=reasoning_effort,
        worker_command=worker_command,
        worker_agent=worker_agent,
        worker_agent_config=worker_agent_config,
    )
    return launch.command


def dispatch_worker(
    task_id,
    attempt_id,
    prompt_path,
    worktree_path=None,
    model="gpt-5.4",
    reasoning_effort="high",
    worker_command="cbc",
    worker_agent="cbc",
    worker_agent_config=None,
    dry_run=True,
    startup_probe_seconds=0.2,
):
    prompt_path = Path(prompt_path)
    try:
        launch = worker_adapters.build_worker_launch(
            prompt_path=prompt_path,
            worktree_path=worktree_path,
            model=model,
            reasoning_effort=reasoning_effort,
            worker_command=worker_command,
            worker_agent=worker_agent,
            worker_agent_config=worker_agent_config,
        )
    except Exception as exc:
        return {
            "ok": False,
            "status": "failed_dispatch",
            "task_id": task_id,
            "attempt_id": attempt_id,
            "pid": None,
            "safe_error": f"worker launch config failed: {exc}",
            "error_type": type(exc).__name__,
            "prompt_path": str(prompt_path),
        }
    command = launch.command
    if dry_run:
        return {
            "ok": True,
            "status": "prepared",
            "task_id": task_id,
            "attempt_id": attempt_id,
            "pid": None,
            "command": command,
            "prompt_path": str(prompt_path),
            "worker_agent": launch.agent,
            "worker_launch": launch.metadata(),
        }

    stdout_path = prompt_path.parent / f"{attempt_id}.stdout.log"
    stderr_path = prompt_path.parent / f"{attempt_id}.stderr.log"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with ExitStack() as stack:
            prompt_file = stack.enter_context(prompt_path.open("r", encoding="utf-8"))
            stdout_file = stack.enter_context(stdout_path.open("a", encoding="utf-8"))
            stderr_file = stack.enter_context(stderr_path.open("a", encoding="utf-8"))
            proc = subprocess.Popen(
                command,
                cwd=launch.cwd,
                env=_worker_environment(launch.environment_allowlist),
                stdin=prompt_file,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
    except Exception as exc:
        return {
            "ok": False,
            "status": "failed_dispatch",
            "task_id": task_id,
            "attempt_id": attempt_id,
            "pid": None,
            "safe_error": f"worker dispatch failed: {exc}",
            "error_type": type(exc).__name__,
            "command": command,
            "prompt_path": str(prompt_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "worker_agent": launch.agent,
            "worker_launch": launch.metadata(),
        }
    if startup_probe_seconds > 0:
        time.sleep(startup_probe_seconds)
    returncode = proc.poll()
    if returncode is not None:
        return {
            "ok": False,
            "status": "failed_dispatch",
            "task_id": task_id,
            "attempt_id": attempt_id,
            "pid": proc.pid,
            "returncode": returncode,
            "safe_error": f"worker exited immediately with code {returncode}",
            "command": command,
            "prompt_path": str(prompt_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "worker_agent": launch.agent,
            "worker_launch": launch.metadata(),
        }
    _BACKGROUND_PROCESSES[proc.pid] = proc
    return {
        "ok": True,
        "status": "running",
        "task_id": task_id,
        "attempt_id": attempt_id,
        "pid": proc.pid,
        "command": command,
        "prompt_path": str(prompt_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "worker_agent": launch.agent,
        "worker_launch": launch.metadata(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--prompt-path", required=True)
    parser.add_argument("--worktree-path")
    parser.add_argument("--model", default="gpt-5.4")
    parser.add_argument("--reasoning-effort", default="high")
    parser.add_argument("--worker-command", default="cbc")
    parser.add_argument(
        "--worker-agent",
        default="cbc",
        choices=worker_adapters.SUPPORTED_WORKER_AGENTS,
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    result = dispatch_worker(
        task_id=args.task_id,
        attempt_id=args.attempt_id,
        prompt_path=args.prompt_path,
        worktree_path=args.worktree_path,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        worker_command=args.worker_command,
        worker_agent=args.worker_agent,
        dry_run=args.dry_run,
    )
    return emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
