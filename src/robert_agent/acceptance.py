#!/usr/bin/env python3
import argparse
import re
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys

from robert_agent.common import emit
from robert_agent import validate_config
from robert_agent.resource_files import resource


AGENT_ROOT = Path(__file__).resolve().parent


def _gate(gate_id, status, summary, next_action=None, evidence=None):
    gate = {
        "id": gate_id,
        "status": status,
        "summary": summary,
    }
    if next_action:
        gate["next_action"] = next_action
    if evidence is not None:
        gate["evidence"] = evidence
    return gate


def _status(gates):
    statuses = {gate["status"] for gate in gates}
    if "failed" in statuses:
        return "blocked"
    if "skipped" in statuses:
        return "incomplete"
    return "ready"


def _next_actions(gates):
    actions = []
    for gate in gates:
        action = gate.get("next_action")
        if action and action not in actions:
            actions.append(action)
    return actions


def _command_path(command_exists, command):
    value = command_exists(command)
    return str(value) if value else ""


def _config_next_action(config_path, safe_error):
    if "No such file or directory" in safe_error:
        script_path = AGENT_ROOT / "init_config.py"
        config_path = Path(config_path).expanduser().resolve()
        return (
            f"{shlex.quote(sys.executable)} -B "
            f"{shlex.quote(str(script_path))} "
            f"--config {shlex.quote(str(config_path))}"
        )
    if "placeholder values" in safe_error:
        return f"Replace placeholder values in {Path(config_path).expanduser().resolve()}"
    if "data_dir is not a directory" in safe_error:
        return f"Set data_dir to a directory path in {Path(config_path).expanduser().resolve()}"
    if "database must not be empty" in safe_error or "database must be a filename" in safe_error:
        return f"Set database to a SQLite filename in {Path(config_path).expanduser().resolve()}"
    if "python_bin must not be empty" in safe_error:
        return f"Set python_bin to python3 or an absolute Python executable in {Path(config_path).expanduser().resolve()}"
    if (
        "workers" in safe_error
        or "worker_agent must not be empty" in safe_error
        or "unsupported worker_agent" in safe_error
    ):
        return f"Fix the named worker definitions in {Path(config_path).expanduser().resolve()}"
    if "worker_command must not be empty" in safe_error:
        return f"Set the worker command to an agent launcher in {Path(config_path).expanduser().resolve()}"
    if "route_worker_models" in safe_error:
        return (
            "Set each route_worker_models entry to a known worker and optional model/effort overrides in "
            f"{Path(config_path).expanduser().resolve()}"
        )
    for field in [
        "max_concurrency",
        "stale_after_minutes",
        "hard_timeout_minutes",
        "worker_startup_grace_seconds",
        "lease_ttl_minutes",
    ]:
        if f"{field} must be at least 1" in safe_error:
            return f"Set {field} to a positive integer in {Path(config_path).expanduser().resolve()}"
    if "repo_root is not a git checkout" in safe_error:
        repo_match = re.search(r"\brepos\[(\d+)\]\.repo_root\b", safe_error)
        repo_index = repo_match.group(1) if repo_match else "0"
        return (
            f"Set repos[{repo_index}].repo_root to a real local git checkout in "
            f"{Path(config_path).expanduser().resolve()}"
        )
    return "Fix robert config"


def _control_plane_state_gate(db_path):
    db_path = Path(db_path).expanduser()
    if not db_path.exists():
        return _gate(
            "control_plane_state",
            "passed",
            "control plane database has not been created yet",
            evidence=str(db_path),
        )
    with sqlite3.connect(db_path) as conn:
        prepared_attempts = conn.execute(
            """
            SELECT COUNT(*)
            FROM attempts a
            JOIN tasks t ON t.task_id = a.task_id
            WHERE a.status = 'prepared'
              AND t.lifecycle IN ('queued', 'running')
            """
        ).fetchone()[0]
        running_attempts = conn.execute(
            """
            SELECT COUNT(*)
            FROM attempts a
            JOIN tasks t ON t.task_id = a.task_id
            WHERE a.status IN ('running', 'stale')
              AND t.lifecycle IN ('queued', 'running')
            """
        ).fetchone()[0]
        pending_publish_actions = conn.execute(
            """
            SELECT COUNT(*)
            FROM github_actions
            WHERE audit_status = 'accepted'
              AND publish_status = 'not_published'
            """
        ).fetchone()[0]
    evidence = {
        "db_path": str(db_path),
        "prepared_attempts": prepared_attempts,
        "running_attempts": running_attempts,
        "pending_publish_actions": pending_publish_actions,
    }
    if prepared_attempts:
        summary = (
            f"{prepared_attempts} prepared attempt"
            f"{'s' if prepared_attempts != 1 else ''} will be eligible for dispatch "
            "on the next non-dry-run cycle"
        )
        summarize_path = AGENT_ROOT / "summarize.py"
        return _gate(
            "control_plane_state",
            "passed",
            summary,
            (
                f"Review prepared attempts before non-dry-run: {sys.executable} -B "
                f"{summarize_path} --db {db_path}"
            ),
            evidence,
        )
    return _gate(
        "control_plane_state",
        "passed",
        "control plane has no prepared attempts",
        evidence=evidence,
    )


def acceptance_preflight(
    config_path,
    skip_external=False,
    command_exists=shutil.which,
    run_command=subprocess.run,
):
    gates = []
    matrix_path = resource("acceptance-matrix.md")
    gates.append(
        _gate(
            "acceptance_matrix",
            "passed" if matrix_path.is_file() else "failed",
            "acceptance matrix is present" if matrix_path.is_file() else "acceptance matrix is missing",
            None if matrix_path.is_file() else "Restore resources/acceptance-matrix.md",
            str(matrix_path),
        )
    )

    config = validate_config.validate_config(config_path, skip_external=True)
    if not config["ok"]:
        safe_error = config["safe_error"]
        gates.append(
            _gate(
                "config_valid",
                "failed",
                safe_error,
                _config_next_action(config_path, safe_error),
            )
        )
        return {
            "ok": False,
            "status": "failed_config",
            "gates": gates,
            "next_actions": _next_actions(gates),
        }

    gates.append(
        _gate(
            "config_valid",
            "passed",
            "config parsed and repository entry is valid",
            evidence=config["config_path"],
        )
    )
    gates.append(_control_plane_state_gate(config["db_path"]))
    multi_repo = len(config["repos"]) > 1
    for repo in config["repos"]:
        repo_name = repo["full_name"]
        repo_root = Path(repo["repo_root"])
        worktree_root = Path(repo["worktree_root"])
        worktree_root_is_dir = worktree_root.is_dir()
        worktree_root_exists = worktree_root.exists()
        repo_checkout_gate_id = f"repo_checkout:{repo_name}" if multi_repo else "repo_checkout"
        worktree_root_gate_id = f"worktree_root:{repo_name}" if multi_repo else "worktree_root"
        gates.append(
            _gate(
                repo_checkout_gate_id,
                "passed" if (repo_root / ".git").exists() else "failed",
                "repo_root is a git checkout"
                if (repo_root / ".git").exists()
                else "repo_root is not a git checkout",
                None if (repo_root / ".git").exists() else f"Provide a real local git checkout for {repo_name}",
                str(repo_root),
            )
        )
        gates.append(
            _gate(
                worktree_root_gate_id,
                "passed" if worktree_root_is_dir else "failed",
                (
                    "worktree_root exists"
                    if worktree_root_is_dir
                    else "worktree_root is not a directory"
                    if worktree_root_exists
                    else "worktree_root is missing"
                ),
                (
                    None
                    if worktree_root_is_dir
                    else (
                        f"Remove file or set repo {repo_name} worktree_root to a directory: {worktree_root}"
                        if multi_repo
                        else f"Remove file or set repos[0].worktree_root to a directory: {worktree_root}"
                    )
                    if worktree_root_exists
                    else f"mkdir -p {shlex.quote(str(worktree_root))}"
                ),
                str(worktree_root),
            )
        )

    python_bin = config.get("python_bin", "python3")
    python_path = _command_path(command_exists, python_bin)
    gates.append(
        _gate(
            "python_bin",
            "passed" if python_path else "failed",
            (
                f"{python_bin} Python command is available"
                if python_path
                else f"{python_bin} Python command is missing"
            ),
            None if python_path else f"Install or configure python_bin: {python_bin}",
            python_path or None,
        )
    )

    gh_path = _command_path(command_exists, "gh")
    gates.append(
        _gate(
            "gh_cli",
            "passed" if gh_path else "failed",
            "gh CLI is available" if gh_path else "gh CLI is missing",
            None if gh_path else "Install GitHub CLI gh",
            gh_path or None,
        )
    )
    if skip_external:
        gates.append(
            _gate(
                "gh_auth",
                "skipped",
                "live GitHub auth check skipped",
                "Run without --skip-external for live GitHub auth verification",
            )
        )
    elif not gh_path:
        gates.append(
            _gate(
                "gh_auth",
                "failed",
                "cannot check GitHub auth without gh CLI",
                "Install GitHub CLI gh",
            )
        )
    else:
        auth = run_command(
            ["gh", "auth", "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        gates.append(
            _gate(
                "gh_auth",
                "passed" if auth.returncode == 0 else "failed",
                "gh authentication is valid" if auth.returncode == 0 else "gh authentication is not ready",
                None if auth.returncode == 0 else "Run gh auth login",
                getattr(auth, "stderr", ""),
            )
        )

    workers = config.get("workers") or [
        {
            "name": "default",
            **config.get("worker_agent_config", {}),
        }
    ]
    multiple_workers = len(workers) > 1
    for worker in workers:
        worker_name = worker["name"]
        worker_agent = worker["agent"]
        worker_executable = worker["command_argv"][0]
        worker_path = _command_path(command_exists, worker_executable)
        gate_id = (
            f"worker_command:{worker_name}"
            if multiple_workers
            else "worker_command"
        )
        summary_prefix = (
            f"{worker_name} ({worker_agent}) worker command"
            if multiple_workers
            else f"{worker_agent} worker agent command"
        )
        gates.append(
            _gate(
                gate_id,
                "passed" if worker_path else "failed",
                (
                    f"{summary_prefix} is available"
                    if worker_path
                    else f"{summary_prefix} is missing"
                ),
                None
                if worker_path
                else (
                    (
                        f"Install or configure {worker_name} ({worker_agent}) "
                        f"worker command: {worker_executable}"
                    )
                    if multiple_workers
                    else (
                        f"Install or configure {worker_agent} worker agent "
                        f"command: {worker_executable}"
                    )
                ),
                worker_path or None,
            )
        )

    status = _status(gates)
    return {
        "ok": status == "ready",
        "status": status,
        "gates": gates,
        "next_actions": _next_actions(gates),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--skip-external", action="store_true")
    args = parser.parse_args(argv)
    result = acceptance_preflight(args.config, skip_external=args.skip_external)
    if result["status"] == "failed_config":
        code = 3
    else:
        code = 0 if result["ok"] else 2
    return emit(result, code)


if __name__ == "__main__":
    raise SystemExit(main())
