#!/usr/bin/env python3
import argparse
from contextlib import closing
import json
import re
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from robert_agent.common import emit
from robert_agent import storage
from robert_agent import publish


TARGET_RE = re.compile(r"^/([^/]+)/([^/]+)/(issues|pull)/([0-9]+)(?:/)?$")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _workspace_dir(workspace_dir):
    if workspace_dir:
        path = Path(workspace_dir).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(tempfile.mkdtemp(prefix="dd-production-write-canary-")).resolve()


def _parse_target_url(target_url):
    parsed = urlparse(target_url or "")
    if parsed.scheme != "https" or parsed.netloc != "github.com":
        return None
    match = TARGET_RE.match(parsed.path)
    if not match:
        return None
    owner, repo, source_type, number = match.groups()
    full_name = f"{owner}/{repo}"
    source_kind = "issue" if source_type == "issues" else "pull_request"
    source_key = f"github:{full_name}{'#' if source_kind == 'issue' else '!'}{number}"
    return {
        "repo": full_name,
        "source_type": source_kind,
        "number": int(number),
        "source_key": source_key,
        "html_url": f"https://github.com/{full_name}/{source_type}/{number}",
    }


def comment_body(marker_id):
    return (
        "<!-- robert-comment task_id=task-production-canary "
        "attempt_id=attempt-production-canary "
        f"event_fingerprints=production-canary:{marker_id} -->\n"
        "Robert production write canary.\n\n"
        "This comment verifies that the configured publisher can create or "
        "deduplicate one GitHub issue/PR comment through the audited action path."
    )


def _insert_canary_fixture(db_path, target, marker_id):
    storage.init_database(db_path)
    now = _now()
    body = comment_body(marker_id)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            """
            INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root)
            VALUES ('repo-production-canary', ?, 'robert', 'main', '', '')
            """,
            (target["repo"],),
        )
        conn.execute(
            """
            INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, html_url)
            VALUES ('source-production-canary', 'repo-production-canary', ?, ?, ?, ?)
            """,
            (target["source_key"], target["source_type"], target["number"], target["html_url"]),
        )
        conn.execute(
            """
            INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, lifecycle, created_at, updated_at)
            VALUES (?, 'repo-production-canary', 'source-production-canary', 'completed', ?, ?)
            """,
            (target["source_key"], now, now),
        )
        conn.execute(
            """
            INSERT INTO workstream_sources(workstream_id, source_id, relationship, created_at)
            VALUES (?, 'source-production-canary', 'primary', ?)
            """,
            (target["source_key"], now),
        )
        conn.execute(
            """
            INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, route_id, expected_output, created_at, updated_at)
            VALUES (
              'task-production-canary', ?, 'completed', 'P2',
              'production-write-canary', 'comment_analysis', ?, ?
            )
            """,
            (target["source_key"], now, now),
        )
        conn.execute(
            """
            INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, heartbeat_at, finished_at)
            VALUES (
              'attempt-production-canary', 'task-production-canary', 1,
              'completed', ?, ?, ?
            )
            """,
            (now, now, now),
        )
        conn.execute(
            """
            INSERT INTO worker_results(
              result_id, task_id, attempt_id, output_type,
              consumed_event_fingerprints_json, verification_json, handoff,
              created_at, metadata_json
            )
            VALUES (
              'result-production-canary', 'task-production-canary',
              'attempt-production-canary', 'comment_analysis',
              ?, '[]', 'production write canary', ?, '{}'
            )
            """,
            (json.dumps([f"production-canary:{marker_id}"]), now),
        )
        conn.execute(
            """
            INSERT INTO github_actions(
              action_id, result_id, task_id, action_type, target_url,
              audit_status, publish_status, created_at, metadata_json
            )
            VALUES (
              'action-production-canary', 'result-production-canary',
              'task-production-canary', 'comment', ?,
              'accepted', 'not_published', ?, ?
            )
            """,
            (
                target["html_url"],
                now,
                json.dumps({"body": body, "marker_id": marker_id}, sort_keys=True),
            ),
        )
    return body


def _action_state(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            """
            SELECT publish_status, external_id, target_url, metadata_json
            FROM github_actions
            WHERE action_id = 'action-production-canary'
            """
        ).fetchone()
    if not row:
        return {}
    metadata = json.loads(row[3] or "{}")
    return {
        "publish_status": row[0],
        "external_id": row[1] or "",
        "target_url": row[2] or "",
        "deduplicated": bool((metadata.get("publish") or {}).get("deduplicated")),
    }


def production_write_canary(
    target_url,
    workspace_dir=None,
    marker_id=None,
    confirm_github_write=False,
    run_command=subprocess.run,
):
    target = _parse_target_url(target_url)
    if not target:
        return {
            "ok": False,
            "status": "invalid_target_url",
            "safe_error": "target_url must be a https://github.com/<owner>/<repo>/issues|pull/<number> URL",
        }
    marker_id = marker_id or uuid4().hex[:12]
    workspace = _workspace_dir(workspace_dir)
    db_path = workspace / "dd.sqlite3"
    body = _insert_canary_fixture(db_path, target, marker_id)
    if not confirm_github_write:
        publish_result = publish.publish_ready_actions(db_path, dry_run=True)
        return {
            "ok": True,
            "status": "planned",
            "write_confirmed": False,
            "workspace_dir": str(workspace),
            "db_path": str(db_path),
            "target_url": target["html_url"],
            "marker_id": marker_id,
            "comment_body": body,
            "publish_result": publish_result,
            "next_action": (
                "Rerun with --confirm-github-write to create or deduplicate the "
                "canary comment through the real publisher."
            ),
        }

    publish_result = publish.publish_ready_actions(
        db_path,
        dry_run=False,
        run_command=run_command,
    )
    action_state = _action_state(db_path)
    status = "publish_failed"
    if publish_result.get("ok"):
        status = "deduplicated" if publish_result.get("deduplicated_count") else "published"
    return {
        "ok": bool(publish_result.get("ok")),
        "status": status,
        "write_confirmed": True,
        "workspace_dir": str(workspace),
        "db_path": str(db_path),
        "target_url": target["html_url"],
        "marker_id": marker_id,
        "comment_body": body,
        "publish_result": publish_result,
        "action_state": action_state,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--workspace-dir")
    parser.add_argument("--marker-id")
    parser.add_argument("--confirm-github-write", action="store_true")
    args = parser.parse_args(argv)
    result = production_write_canary(
        args.target_url,
        workspace_dir=args.workspace_dir,
        marker_id=args.marker_id,
        confirm_github_write=args.confirm_github_write,
    )
    if result["status"] == "invalid_target_url":
        return emit(result, 3)
    if result.get("ok"):
        return emit(result, 0)
    return emit(result, 2)


if __name__ == "__main__":
    raise SystemExit(main())
