#!/usr/bin/env python3
import argparse
from contextlib import closing
import json
import os
import signal
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from robert_agent.common import emit
from robert_agent import run_once
from robert_agent import validate_config
from robert_agent.resource_files import resource


DEFAULT_WORKFLOW = resource("workflow.yml")


def _acceptance_id():
    return f"worktree-acceptance-{uuid4().hex[:12]}"


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _workspace_dir(workspace_dir):
    if workspace_dir:
        path = Path(workspace_dir).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(tempfile.mkdtemp(prefix="dd-worktree-acceptance-")).resolve()


def _run(args, cwd=None):
    completed = subprocess.run(
        args,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return {
        "args": [str(arg) for arg in args],
        "cwd": str(cwd) if cwd else "",
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _controlled_checkout(workspace_dir):
    source = workspace_dir / "upstream-source"
    bare = workspace_dir / "upstream.git"
    checkout = workspace_dir / "checkout"
    commands = []
    source.mkdir(parents=True, exist_ok=True)
    commands.append(_run(["git", "init", "-b", "main"], cwd=source))
    (source / "README.md").write_text("# acceptance\n", encoding="utf-8")
    commands.append(_run(["git", "add", "README.md"], cwd=source))
    commands.append(
        _run(
            [
                "git",
                "-c",
                "user.name=DD Acceptance",
                "-c",
                "user.email=dd-acceptance@example.invalid",
                "commit",
                "-m",
                "initial acceptance fixture",
            ],
            cwd=source,
        )
    )
    commands.append(_run(["git", "clone", "--bare", str(source), str(bare)]))
    commands.append(_run(["git", "clone", str(bare), str(checkout)]))
    commands.append(_run(["git", "remote", "rename", "origin", "upstream"], cwd=checkout))
    worktree_root = checkout / ".worktrees"
    worktree_root.mkdir(parents=True, exist_ok=True)
    return {
        "repo_root": checkout,
        "worktree_root": worktree_root,
        "upstream": bare,
        "commands": commands,
    }


def _mock_worker(workspace_dir):
    worker = workspace_dir / "mock-worker.py"
    worker.write_text(
        """#!/usr/bin/env python3
import time

time.sleep(30)
""",
        encoding="utf-8",
    )
    os.chmod(worker, 0o755)
    return worker


def _config(source_config_path, workspace_dir, checkout, worker):
    source_config = validate_config.validate_config(source_config_path, skip_external=True)
    if not source_config["ok"]:
        return source_config
    repo = source_config["repos"][0]
    controlled = {
        "data_dir": str(workspace_dir / "data"),
        "database": "dd.sqlite3",
        "worker_command": str(worker),
        "max_concurrency": 1,
        "worker_startup_grace_seconds": 30,
        "stale_after_minutes": source_config.get("stale_after_minutes", 20),
        "hard_timeout_minutes": source_config.get("hard_timeout_minutes", 90),
        "lease_ttl_minutes": source_config.get("lease_ttl_minutes", 9),
        "repos": [
            {
                "full_name": repo["full_name"],
                "github_account": repo["github_account"],
                "trusted_actors": repo["trusted_actors"],
                "default_base_branch": "main",
                "repo_root": str(checkout["repo_root"]),
                "worktree_root": str(checkout["worktree_root"]),
            }
        ],
    }
    config_path = workspace_dir / "config.json"
    _write_json(config_path, controlled)
    return {
        "ok": True,
        "status": "prepared",
        "config_path": config_path,
        "db_path": workspace_dir / "data" / "dd.sqlite3",
        "repo": controlled["repos"][0],
    }


def _fixture(config, workspace_dir, issue_number=77):
    repo = config["repo"]
    fixture_path = workspace_dir / "fixture.json"
    _write_json(
        fixture_path,
        {
            "events": [
                {
                    "id": _acceptance_id(),
                    "number": issue_number,
                    "source_type": "issue",
                    "event_type": "comment",
                    "actor_login": repo["trusted_actors"][0],
                    "author_association": "OWNER",
                    "title": "Fix worktree acceptance",
                    "body": f"@{repo['github_account']} please fix this controlled worktree acceptance bug",
                    "intent": "bug_fix",
                    "url": f"https://github.com/{repo['full_name']}/issues/{issue_number}",
                }
            ]
        },
    )
    return fixture_path


def _json_load(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _attempt_evidence(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            """
            SELECT
              t.route_id,
              t.lifecycle,
              a.attempt_id,
              a.status,
              a.worktree_path,
              a.branch_name,
              a.metadata_json
            FROM attempts a
            JOIN tasks t ON t.task_id = a.task_id
            ORDER BY a.started_at DESC, a.attempt_id DESC
            LIMIT 1
            """
        ).fetchone()
    if not row:
        return {}
    route_id, task_lifecycle, attempt_id, attempt_status, worktree_path, branch_name, metadata_json = row
    metadata = _json_load(metadata_json)
    return {
        "route_id": route_id,
        "task_lifecycle": task_lifecycle,
        "attempt_id": attempt_id,
        "attempt_status": attempt_status,
        "worktree_path": worktree_path,
        "branch_name": branch_name,
        "dispatch": metadata.get("dispatch") or {},
    }


def _git_branch(worktree_path):
    return subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=worktree_path,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _worktree_list_contains_branch(repo_root, branch_name):
    completed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=True,
    )
    return f"branch refs/heads/{branch_name}" in completed.stdout


def _terminate_worker(dispatch_result):
    pid = dispatch_result.get("pid")
    if not pid:
        return {"status": "not_signalled", "reason": "missing_pid"}
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {"status": "not_signalled", "reason": "invalid_pid", "pid": pid}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"status": "not_found", "pid": pid}
    proc = run_once.dispatch._BACKGROUND_PROCESSES.get(pid)
    if proc:
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return {"status": "signalled", "pid": pid}
        return {"status": "terminated", "pid": pid}
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return {"status": "terminated", "pid": pid}
        time.sleep(0.05)
    return {"status": "signalled", "pid": pid}


def live_worktree_acceptance(config_path, workspace_dir=None, workflow_path=DEFAULT_WORKFLOW):
    workspace = _workspace_dir(workspace_dir)
    checkout = _controlled_checkout(workspace)
    worker = _mock_worker(workspace)
    config = _config(config_path, workspace, checkout, worker)
    if not config["ok"]:
        return config
    fixture = _fixture(config, workspace)
    result = run_once.run_once(
        config["config_path"],
        workflow_path=workflow_path,
        fixture_path=fixture,
        dry_run=False,
        skip_external=True,
    )
    evidence = _attempt_evidence(config["db_path"])
    cleanup = _terminate_worker(evidence.get("dispatch") or {})
    if not result["ok"]:
        return {
            "ok": False,
            "status": result.get("status", "failed_worktree_acceptance"),
            "safe_error": result.get("safe_error", "worktree acceptance run failed"),
            "workspace_dir": str(workspace),
            "config_path": str(config["config_path"]),
            "fixture_path": str(fixture),
            "db_path": str(config["db_path"]),
            "run_once": result,
            "evidence": evidence,
            "cleanup": cleanup,
        }

    worktree_path = Path(evidence.get("worktree_path") or "")
    branch_name = evidence.get("branch_name") or ""
    git_branch = _git_branch(worktree_path) if worktree_path.exists() else ""
    in_worktree_list = _worktree_list_contains_branch(checkout["repo_root"], branch_name)
    ok = (
        evidence.get("route_id") == "new-pr"
        and evidence.get("attempt_status") == "running"
        and worktree_path.is_dir()
        and git_branch == branch_name
        and in_worktree_list
    )
    return {
        "ok": ok,
        "status": "completed" if ok else "failed_worktree_verification",
        "workspace_dir": str(workspace),
        "config_path": str(config["config_path"]),
        "fixture_path": str(fixture),
        "db_path": str(config["db_path"]),
        "repo_root": str(checkout["repo_root"]),
        "worktree_root": str(checkout["worktree_root"]),
        "route_id": evidence.get("route_id"),
        "task_lifecycle": evidence.get("task_lifecycle"),
        "attempt_id": evidence.get("attempt_id"),
        "attempt_status": evidence.get("attempt_status"),
        "worktree_path": str(worktree_path),
        "branch_name": branch_name,
        "git_branch": git_branch,
        "git_worktree_list_contains_branch": in_worktree_list,
        "run_once": result,
        "checkout_commands": checkout["commands"],
        "cleanup": cleanup,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--workspace-dir")
    parser.add_argument("--workflow", default=str(DEFAULT_WORKFLOW))
    args = parser.parse_args(argv)
    result = live_worktree_acceptance(
        args.config,
        workspace_dir=args.workspace_dir,
        workflow_path=Path(args.workflow),
    )
    if result.get("ok"):
        return emit(result, 0)
    if result.get("status") == "failed_config":
        return emit(result, 3)
    return emit(result, 2)


if __name__ == "__main__":
    raise SystemExit(main())
