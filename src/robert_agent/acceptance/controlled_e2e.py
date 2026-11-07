from robert_agent.adapters.processes import attempt as attempt_process

from robert_agent.persistence import database as db_database

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_actions as db_read_github_actions
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import worker_results as db_read_worker_results
from robert_agent.persistence.queries import workstreams as db_read_workstreams

#!/usr/bin/env python3
import argparse
from contextlib import closing
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from robert_agent.foundation.commands import emit
from robert_agent.acceptance import worktree as live_worktree_acceptance
from robert_agent.runtime import cycle as run_once
from robert_agent.configuration import command as validate_config
from robert_agent.resource_files import resource
from robert_agent.delivery.application import publish


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKFLOW = resource("workflow.yml")


def _workspace_dir(workspace_dir):
    if workspace_dir:
        path = Path(workspace_dir).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(tempfile.mkdtemp(prefix="dd-controlled-e2e-")).resolve()


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _mock_new_pr_worker(workspace_dir):
    worker = workspace_dir / "mock-new-pr-worker.py"
    worker.write_text(
        f"""#!/usr/bin/env python3
import json
import sys

sys.path.insert(0, {str(PACKAGE_ROOT.parent)!r})
from robert_agent.delivery.application import submission as result

prompt = sys.stdin.read()
fields = {{}}
for line in prompt.splitlines():
    if ": " in line:
        key, value = line.split(": ", 1)
        fields[key] = value
fingerprints = json.loads(fields["event_fingerprints"])
submission = {{
    "planned_github_actions": [
        {{
            "type": "push_existing_pr",
        }},
        {{
            "type": "open_pr",
            "title": "Controlled acceptance PR",
            "issue_resolution": "complete",
            "body": "Controlled acceptance PR",
        }}
    ],
    "consumed_event_fingerprints": fingerprints,
    "verification": [
        {{
            "command": "controlled-e2e-worker",
            "status": "passed",
            "purpose": "record controlled e2e worker acceptance result",
            "required": True,
            "source": "worker_reported",
            "exit_code": 0,
        }}
    ],
    "handoff": "controlled e2e ready",
    "used_skills": [],
}}
payload = {{"task_id": fields["task_id"], "attempt_id": fields["attempt_id"],
           "output_type": "new_pr", "submission": submission}}
record = result.record_result(fields["db_path"], payload)
print(json.dumps(record, sort_keys=True))
raise SystemExit(0 if record["ok"] else 1)
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
    config = {
        "version": 2,
        "default_worker": "default",
        "data_dir": str(workspace_dir / "data"),
        "database": "dd.sqlite3",
        "github": {"account": repo["github_account"]},
        "workers": {
            "default": {
                "adapter": "cbc",
                "command": [sys.executable, str(worker)],
                "model": "default",
                "effort": "default",
            }
        },
        "scheduler": {"max_concurrency": 1, "lease_ttl_seconds": source_config["lease_ttl_seconds"]},
        "supervision": {
            "startup_grace_seconds": 1,
            "heartbeat_stale_seconds": source_config["heartbeat_stale_seconds"],
            "hard_timeout_seconds": source_config["hard_timeout_seconds"],
        },
        "repos": [
            {
                "full_name": repo["full_name"],
                "trusted_actors": repo["trusted_actors"],
                "default_branch": "main",
                "checkout": str(checkout["repo_root"]),
                "worktrees": str(checkout["worktree_root"]),
            }
        ],
    }
    config_path = workspace_dir / "config.json"
    _write_json(config_path, config)
    return {
        "ok": True,
        "status": "prepared",
        "config_path": config_path,
        "db_path": workspace_dir / "data" / "dd.sqlite3",
        "repo": repo | {"repo_root": str(checkout["repo_root"]), "worktree_root": str(checkout["worktree_root"])},
    }


def _fixture(config, workspace_dir):
    repo = config["repo"]
    fixture_path = workspace_dir / "fixture.json"
    _write_json(
        fixture_path,
        {
            "events": [
                {
                    "id": "controlled-e2e-comment",
                    "number": 77,
                    "source_type": "issue",
                    "event_type": "comment",
                    "actor_login": repo["trusted_actors"][0],
                    "author_association": "OWNER",
                    "title": "Fix controlled e2e acceptance",
                    "body": f"@{repo['github_account']} please fix this controlled e2e bug",
                    "intent": "bug_fix",
                    "url": f"https://github.com/{repo['full_name']}/issues/77",
                }
            ]
        },
    )
    return fixture_path


def _wait_for_result(db_path, timeout_seconds, poll_interval_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        with closing(db_database.connect(db_path)) as conn:
            row = db_records.first(db_read_worker_results.find_for_wait_for_result(conn))
        if row and attempt_process.dispatch_status(json.loads(row[2] or "{}"))["status"] == "dead":
            return {"result_id": row[0], "output_type": row[1]}
        time.sleep(poll_interval_seconds)
    return {}


class _PublishRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append({"command": list(command), "kwargs": dict(kwargs)})

        class Completed:
            returncode = 0
            stderr = ""

            def __init__(self, stdout):
                self.stdout = stdout

        if command == ["git", "rev-parse", "HEAD"]:
            return Completed("a" * 40 + "\n")
        if command == ["git", "remote", "get-url", "origin"]:
            return Completed("https://github.com/example/repo.git\n")
        if command[:3] == ["git", "ls-remote", "--heads"]:
            return Completed("")
        if command[:2] == ["git", "push"]:
            return Completed("pushed\n")
        if command[:3] == ["gh", "pr", "list"]:
            return Completed("[]")
        if command[:3] == ["gh", "pr", "create"]:
            repo = command[command.index("--repo") + 1] if "--repo" in command else "example/repo"
            return Completed(f"https://github.com/{repo}/pull/42\n")
        return Completed("[]")


def _evidence(db_path, repo_full_name):
    issue_workstream_id = f"github:{repo_full_name}#77"
    pr_workstream_id = f"github:{repo_full_name}!42"
    with closing(db_database.connect(db_path)) as conn:
        task = db_records.first(db_read_tasks.find_for_evidence(conn))
        action_rows = db_read_github_actions.find_for_evidence(conn)
        issue_stream = db_records.first(db_read_workstreams.find_lifecycle_by_workstream_id(conn, issue_workstream_id))
        pr_stream = db_records.first(db_read_workstreams.find_lifecycle_by_workstream_id(conn, pr_workstream_id))
        result = db_records.first(db_read_worker_results.find_for_evidence(conn))
    return {
        "task_id": task[0] if task else "",
        "task_lifecycle": task[1] if task else "",
        "route_id": task[2] if task else "",
        "worktree_path": task[3] if task else "",
        "branch_name": task[4] if task else "",
        "github_actions": [(row[0], row[1], row[2]) for row in action_rows],
        "published_pr_url": next(
            (row[3] for row in action_rows if row[0] == "open_pr" and row[3]),
            "",
        ),
        "issue_workstream_lifecycle": issue_stream[0] if issue_stream else "",
        "derived_pr_workstream_lifecycle": pr_stream[0] if pr_stream else "",
        "worker_result": {
            "result_id": result[0] if result else "",
            "output_type": result[1] if result else "",
            "metadata": json.loads(result[2] or "{}") if result else {},
        },
    }


def controlled_e2e_acceptance(
    config_path,
    workspace_dir=None,
    timeout_seconds=30,
    poll_interval_seconds=0.2,
    workflow_path=DEFAULT_WORKFLOW,
):
    workspace = _workspace_dir(workspace_dir)
    checkout = live_worktree_acceptance._controlled_checkout(workspace)
    worker = _mock_new_pr_worker(workspace)
    config = _config(config_path, workspace, checkout, worker)
    if not config["ok"]:
        return config
    fixture = _fixture(config, workspace)
    first_run = run_once.run_once(
        config["config_path"],
        workflow_path=workflow_path,
        fixture_path=fixture,
        dry_run=False,
        skip_external=True,
    )
    if not first_run["ok"]:
        return {
            "ok": False,
            "status": first_run.get("status", "failed_first_run"),
            "safe_error": first_run.get("safe_error", "controlled e2e first run failed"),
            "workspace_dir": str(workspace),
            "config_path": str(config["config_path"]),
            "fixture_path": str(fixture),
            "db_path": str(config["db_path"]),
            "first_run": first_run,
        }
    worker_result = _wait_for_result(config["db_path"], timeout_seconds, poll_interval_seconds)
    if not worker_result:
        return {
            "ok": False,
            "status": "worker_result_timeout",
            "safe_error": "controlled e2e worker did not record a result",
            "workspace_dir": str(workspace),
            "config_path": str(config["config_path"]),
            "fixture_path": str(fixture),
            "db_path": str(config["db_path"]),
            "first_run": first_run,
        }

    runner = _PublishRunner()
    original_publish_ready_actions = publish.publish_ready_actions

    def fake_publish_ready_actions(db_path, dry_run=False, repo_id=None):
        return original_publish_ready_actions(
            db_path,
            dry_run=dry_run,
            run_command=runner,
            repo_id=repo_id,
        )

    try:
        publish.publish_ready_actions = fake_publish_ready_actions
        second_run = run_once.run_once(
            config["config_path"],
            workflow_path=workflow_path,
            dry_run=False,
            skip_external=True,
        )
    finally:
        publish.publish_ready_actions = original_publish_ready_actions
    repo_full_name = config["repo"]["full_name"]
    evidence = _evidence(config["db_path"], repo_full_name)
    ok = (
        second_run.get("ok")
        and evidence["route_id"] == "new-pr"
        and evidence["task_lifecycle"] == "completed"
        and evidence["issue_workstream_lifecycle"] == "completed"
        and evidence["derived_pr_workstream_lifecycle"] == "completed"
        and evidence["github_actions"]
        == [
            ("push_existing_pr", "accepted", "published"),
            ("open_pr", "accepted", "published"),
        ]
        and evidence["published_pr_url"] == f"https://github.com/{repo_full_name}/pull/42"
    )
    return {
        "ok": bool(ok),
        "status": "completed" if ok else "failed_e2e_verification",
        "workspace_dir": str(workspace),
        "config_path": str(config["config_path"]),
        "fixture_path": str(fixture),
        "db_path": str(config["db_path"]),
        "first_run": first_run,
        "second_run": second_run,
        "publish_result": second_run.get("publish_result", {}),
        "publish_calls": runner.calls,
        **evidence,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--workspace-dir")
    parser.add_argument("--workflow", default=str(DEFAULT_WORKFLOW))
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--poll-interval-seconds", type=float, default=0.2)
    args = parser.parse_args(argv)
    result = controlled_e2e_acceptance(
        args.config,
        workspace_dir=args.workspace_dir,
        workflow_path=Path(args.workflow),
        timeout_seconds=args.timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
    )
    if result.get("ok"):
        return emit(result, 0)
    if result.get("status") == "failed_config":
        return emit(result, 3)
    return emit(result, 2)


if __name__ == "__main__":
    raise SystemExit(main())
