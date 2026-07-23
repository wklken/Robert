#!/usr/bin/env python3
import argparse
from contextlib import closing
import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from robert_agent.common import emit
from robert_agent import storage
from robert_agent import publish


COMMENT_BODY = (
    "<!-- robert-comment task_id=task-comment attempt_id=attempt-comment "
    "event_fingerprints=comment:1 -->\nExisting comment"
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
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            """
            INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root)
            VALUES ('repo-1', 'x/y', 'robot', 'main', '/repo', '/repo/.worktrees')
            """
        )
        conn.execute(
            """
            INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, html_url)
            VALUES ('source-1', 'repo-1', 'github:x/y#1', 'issue', 1, 'https://github.com/x/y/issues/1')
            """
        )
        conn.execute(
            """
            INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, lifecycle, created_at, updated_at)
            VALUES ('github:x/y#1', 'repo-1', 'source-1', 'active', ?, ?)
            """,
            (now, now),
        )
        conn.executemany(
            """
            INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, created_at, updated_at)
            VALUES (?, 'github:x/y#1', ?, 'P1', ?, ?)
            """,
            [
                ("task-comment", "completed", now, now),
                ("task-pr", "running", now, now),
            ],
        )
        conn.executemany(
            """
            INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, heartbeat_at)
            VALUES (?, ?, 1, 'completed', ?, ?)
            """,
            [
                ("attempt-comment", "task-comment", now, now),
                ("attempt-pr", "task-pr", now, now),
            ],
        )
        conn.executemany(
            """
            INSERT INTO worker_results(
              result_id, task_id, attempt_id, output_type,
              consumed_event_fingerprints_json, verification_json, handoff, created_at
            )
            VALUES (?, ?, ?, ?, '["comment:1"]', '[]', 'ready', ?)
            """,
            [
                ("result-comment", "task-comment", "attempt-comment", "comment_analysis", now),
                ("result-pr", "task-pr", "attempt-pr", "new_pr", now),
            ],
        )
        conn.executemany(
            """
            INSERT INTO github_actions(
              action_id, result_id, task_id, action_type, target_url,
              audit_status, publish_status, created_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, 'accepted', 'not_published', ?, ?)
            """,
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
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT publish_status, metadata_json
            FROM github_actions
            ORDER BY action_id
            """
        ).fetchall()
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
