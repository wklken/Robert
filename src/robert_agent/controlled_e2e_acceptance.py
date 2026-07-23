#!/usr/bin/env python3
import argparse
from contextlib import closing
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from robert_agent.common import emit
from robert_agent import live_worktree_acceptance
from robert_agent import run_once
from robert_agent import validate_config
from robert_agent.resource_files import resource


PACKAGE_ROOT = Path(__file__).resolve().parent
WORKER_RESULT_SCRIPT = PACKAGE_ROOT / "worker_result.py"
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

sys.path.insert(0, {str(WORKER_RESULT_SCRIPT.parent.parent)!r})
from robert_agent.worker import result

prompt = sys.stdin.read()
fields = {{}}
for line in prompt.splitlines():
    if ": " in line:
        key, value = line.split(": ", 1)
        fields[key] = value
fingerprints = json.loads(fields["event_fingerprints"])
workstream_id = fields["workstream_id"]
issue_number = workstream_id.rsplit("#", 1)[-1]
repo = workstream_id[len("github:"):].split("#", 1)[0]
body = (
    "<!-- robert-workstream\\n"
    "origin_workstream_id: {{}}\\n"
    "source_issue: {{}}\\n"
    "task_id: {{}}\\n"
    "created_by: robert\\n"
    "-->\\nControlled acceptance PR"
).format(workstream_id, issue_number, fields["task_id"])
payload = {{
    "task_id": fields["task_id"],
    "attempt_id": fields["attempt_id"],
    "output_type": "new_pr",
    "planned_github_actions": [
        {{
            "type": "push_existing_pr",
            "worktree_path": fields["worktree_path"],
            "branch": fields["branch_name"],
        }},
        {{
            "type": "open_pr",
            "repo": repo,
            "head": fields["branch_name"],
            "base": fields["target_base_branch"],
            "title": "Controlled acceptance PR",
            "body": body,
        }}
    ],
    "consumed_event_fingerprints": fingerprints,
    "verification": [
        {{
            "command": "controlled-e2e-worker",
            "status": "passed",
            "purpose": "record controlled e2e worker acceptance result",
            "required": True,
            "exit_code": 0,
        }}
    ],
    "handoff": "controlled e2e ready",
    "used_skills": [],
}}
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
        "data_dir": str(workspace_dir / "data"),
        "database": "dd.sqlite3",
        "worker_command": str(worker),
        "max_concurrency": 1,
        "worker_startup_grace_seconds": 1,
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
    _write_json(config_path, config)
    return {
        "ok": True,
        "status": "prepared",
        "config_path": config_path,
        "db_path": workspace_dir / "data" / "dd.sqlite3",
        "repo": config["repos"][0],
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
        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute(
                """
                SELECT result_id, output_type
                FROM worker_results
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
        if row:
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

        if command[:3] == ["gh", "pr", "list"]:
            return Completed("[]")
        if command[:3] == ["gh", "pr", "create"]:
            repo = command[command.index("--repo") + 1] if "--repo" in command else "example/repo"
            return Completed(f"https://github.com/{repo}/pull/42\n")
        return Completed("[]")


def _evidence(db_path, repo_full_name):
    issue_workstream_id = f"github:{repo_full_name}#77"
    pr_workstream_id = f"github:{repo_full_name}!42"
    with closing(sqlite3.connect(db_path)) as conn:
        task = conn.execute(
            """
            SELECT t.task_id, t.lifecycle, t.route_id, a.worktree_path, a.branch_name
            FROM tasks t
            JOIN attempts a ON a.task_id = t.task_id
            ORDER BY t.created_at DESC
            LIMIT 1
            """
        ).fetchone()
        action_rows = conn.execute(
            """
            SELECT action_type, audit_status, publish_status, target_url
            FROM github_actions
            ORDER BY created_at, action_id
            """
        ).fetchall()
        issue_stream = conn.execute(
            """
            SELECT lifecycle
            FROM workstreams
            WHERE workstream_id = ?
            """
            ,
            (issue_workstream_id,),
        ).fetchone()
        pr_stream = conn.execute(
            """
            SELECT lifecycle
            FROM workstreams
            WHERE workstream_id = ?
            """
            ,
            (pr_workstream_id,),
        ).fetchone()
        result = conn.execute(
            """
            SELECT result_id, output_type, metadata_json
            FROM worker_results
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
    return {
        "task_id": task[0] if task else "",
        "task_lifecycle": task[1] if task else "",
        "route_id": task[2] if task else "",
        "worktree_path": task[3] if task else "",
        "branch_name": task[4] if task else "",
        "github_actions": [
            (row[0], row[1], row[2]) for row in action_rows
        ],
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
    original_publish_ready_actions = run_once.publish.publish_ready_actions

    def fake_publish_ready_actions(db_path, dry_run=False, repo_id=None):
        return original_publish_ready_actions(
            db_path,
            dry_run=dry_run,
            run_command=runner,
            repo_id=repo_id,
        )

    try:
        run_once.publish.publish_ready_actions = fake_publish_ready_actions
        second_run = run_once.run_once(
            config["config_path"],
            workflow_path=workflow_path,
            dry_run=False,
            skip_external=True,
        )
    finally:
        run_once.publish.publish_ready_actions = original_publish_ready_actions
    repo_full_name = config["repo"]["full_name"]
    evidence = _evidence(config["db_path"], repo_full_name)
    ok = (
        second_run.get("ok")
        and evidence["route_id"] == "new-pr"
        and evidence["task_lifecycle"] == "completed"
        and evidence["issue_workstream_lifecycle"] == "completed"
        and evidence["derived_pr_workstream_lifecycle"] == "completed"
        and evidence["github_actions"] == [
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
