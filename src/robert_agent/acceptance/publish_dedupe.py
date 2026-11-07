from robert_agent.persistence.stores import attempts as db_write_attempts
from robert_agent.persistence.stores import github_actions as db_write_github_actions
from robert_agent.persistence.stores import tasks as db_write_tasks
from robert_agent.persistence.stores import worker_results as db_write_worker_results

from robert_agent.persistence import database as db_database

from robert_agent.persistence.queries import github_actions as db_read_github_actions
from robert_agent.persistence.stores import github_sources as db_write_github_sources
from robert_agent.persistence.stores import repos as db_write_repos
from robert_agent.persistence.stores import workstreams as db_write_workstreams

#!/usr/bin/env python3
import argparse
from contextlib import closing
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from robert_agent.foundation.commands import emit
from robert_agent.persistence.schema import database as storage
from robert_agent.delivery.application import publish


COMMENT_BODY = (
    "<!-- robert-comment task_id=task-comment attempt_id=attempt-comment action_index=0 -->\nExisting comment"
)
PR_BODY = (
    "<!-- robert-workstream\n"
    "origin_workstream_id: github:x/y#1\n"
    "source_issue: 1\n"
    "task_id: task-pr\n"
    "created_by: robert\n"
    "-->\nExisting PR"
)
EXISTING_PR_BODY_WITH_DIFFERENT_TASK = (
    "<!-- robert-workstream\n"
    "origin_workstream_id: github:x/y#1\n"
    "source_issue: 1\n"
    "task_id: task-pr-old\n"
    "created_by: robert\n"
    "-->\nExisting PR"
)


class _Completed:
    returncode = 0
    stderr = ""

    def __init__(self, stdout):
        self.stdout = stdout


class _DedupeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append({"command": list(command), "kwargs": dict(kwargs)})
        if command == ["gh", "api", "repos/x/y/issues/1/comments?per_page=100", "--paginate", "--slurp"]:
            return _Completed(
                json.dumps(
                    [
                        [
                            {
                                "id": 987,
                                "html_url": "https://github.com/x/y/issues/1#issuecomment-987",
                                "body": COMMENT_BODY,
                            }
                        ]
                    ]
                )
            )
        if command[:3] == ["gh", "pr", "list"]:
            return _Completed(
                json.dumps(
                    [
                        {
                            "number": 42,
                            "url": "https://github.com/x/y/pull/42",
                            "baseRefName": "main",
                            "headRefName": "codex/dd-1",
                            "headRepositoryOwner": {"login": "x"},
                        }
                    ]
                )
            )
        if command == [
            "gh",
            "pr",
            "view",
            "42",
            "--repo",
            "x/y",
            "--json",
            "number,url,body,baseRefName",
        ]:
            return _Completed(
                json.dumps(
                    {
                        "number": 42,
                        "url": "https://github.com/x/y/pull/42",
                        "body": EXISTING_PR_BODY_WITH_DIFFERENT_TASK,
                        "baseRefName": "main",
                        "headRefName": "codex/dd-1",
                        "headRepositoryOwner": {"login": "x"},
                    },
                    sort_keys=True,
                )
            )
        return _Completed("")


def _workspace_dir(workspace_dir):
    if workspace_dir:
        path = Path(workspace_dir).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(tempfile.mkdtemp(prefix="dd-publish-dedupe-")).resolve()


def _insert_fixture(db_path):
    storage.init_database(db_path)
    now = datetime.now(timezone.utc).isoformat()
    with closing(db_database.connect(db_path)) as conn, conn:
        db_write_repos.insert_fixture(conn)
        db_write_github_sources.insert_fixture(conn)
        db_write_workstreams.insert_fixture(conn, now, now)
        db_write_tasks.insert_fixture(conn, [("task-comment", "completed", now, now), ("task-pr", "running", now, now)])
        db_write_attempts.insert_fixture(
            conn, [("attempt-comment", "task-comment", now, now), ("attempt-pr", "task-pr", now, now)]
        )
        db_write_worker_results.insert_fixture(
            conn,
            [
                ("result-comment", "task-comment", "attempt-comment", "comment_analysis", now),
                ("result-pr", "task-pr", "attempt-pr", "new_pr", now),
            ],
        )
        db_write_github_actions.insert_fixture(
            conn,
            [
                (
                    "action-comment",
                    "result-comment",
                    "task-comment",
                    "comment",
                    "https://github.com/x/y/issues/1",
                    now,
                    json.dumps({"body": COMMENT_BODY}, sort_keys=True),
                ),
                (
                    "action-pr",
                    "result-pr",
                    "task-pr",
                    "open_pr",
                    None,
                    now,
                    json.dumps(
                        {
                            "repo": "x/y",
                            "head": "codex/dd-1",
                            "head_owner": "x",
                            "base": "main",
                            "title": "Existing PR",
                            "body": PR_BODY,
                        },
                        sort_keys=True,
                    ),
                ),
            ],
        )


def _action_counts(db_path):
    with closing(db_database.connect(db_path)) as conn:
        rows = db_read_github_actions.find_for_action_counts(conn)
    published = 0
    deduplicated = 0
    for publish_status, metadata_json in rows:
        if publish_status == "published":
            published += 1
        metadata = json.loads(metadata_json or "{}")
        if (metadata.get("publish") or {}).get("deduplicated"):
            deduplicated += 1
    return {"published": published, "deduplicated": deduplicated}


def publish_dedupe_acceptance(workspace_dir=None):
    workspace = _workspace_dir(workspace_dir)
    db_path = workspace / "dd.sqlite3"
    _insert_fixture(db_path)
    runner = _DedupeRunner()
    publish_result = publish.publish_ready_actions(db_path, dry_run=False, run_command=runner)
    action_counts = _action_counts(db_path)
    create_commands = [
        call
        for call in runner.calls
        if call["command"][:3] == ["gh", "api", "repos/x/y/issues/1/comments"]
        or call["command"][:3] == ["gh", "pr", "create"]
    ]
    ok = (
        publish_result.get("ok")
        and publish_result.get("published_count") == 2
        and publish_result.get("deduplicated_count") == 2
        and not create_commands
    )
    return {
        "ok": bool(ok),
        "status": "completed" if ok else "failed_dedupe_verification",
        "workspace_dir": str(workspace),
        "db_path": str(db_path),
        "publish_result": publish_result,
        "action_counts": action_counts,
        "gh_calls": runner.calls,
        "create_commands": create_commands,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-dir")
    args = parser.parse_args(argv)
    result = publish_dedupe_acceptance(workspace_dir=args.workspace_dir)
    return emit(result, 0 if result.get("ok") else 2)


if __name__ == "__main__":
    raise SystemExit(main())
