from robert_agent.persistence import database as db_database

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_actions as db_read_github_actions
from robert_agent.persistence.stores import attempts as db_write_attempts
from robert_agent.persistence.stores import github_actions as db_write_github_actions
from robert_agent.persistence.stores import github_sources as db_write_github_sources
from robert_agent.persistence.stores import repos as db_write_repos
from robert_agent.persistence.stores import tasks as db_write_tasks
from robert_agent.persistence.stores import worker_results as db_write_worker_results
from robert_agent.persistence.stores import workstream_sources as db_write_workstream_sources
from robert_agent.persistence.stores import workstreams as db_write_workstreams

#!/usr/bin/env python3
import argparse
from contextlib import closing
import json
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from robert_agent.foundation.commands import emit
from robert_agent.persistence.schema import database as storage
from robert_agent.delivery.application import publish
from robert_agent.adapters.github import client as github_client


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
        f"attempt_id=attempt-production-canary-{marker_id} action_index=0 -->\n"
        "Robert production write canary.\n\n"
        "This comment verifies that the configured publisher can create or "
        "deduplicate one GitHub issue/PR comment through the audited action path."
    )


def _insert_canary_fixture(db_path, target, marker_id, publisher=None):
    storage.init_database(db_path)
    now = _now()
    body = comment_body(marker_id)
    with closing(db_database.connect(db_path)) as conn, conn:
        db_write_repos.insert_canary_fixture(conn, target["repo"])
        db_write_github_sources.insert_canary_fixture(
            conn, target["source_key"], target["source_type"], target["number"], target["html_url"]
        )
        db_write_workstreams.insert_canary_fixture(conn, target["source_key"], now, now)
        db_write_workstream_sources.insert_canary_fixture(conn, target["source_key"], now)
        db_write_tasks.insert_canary_fixture(conn, target["source_key"], now, now)
        db_write_attempts.insert_canary_fixture(conn, f"attempt-production-canary-{marker_id}", now, now, now)
        db_write_worker_results.insert_canary_fixture(
            conn,
            f"attempt-production-canary-{marker_id}",
            json.dumps([f"production-canary:{marker_id}"]),
            now,
            json.dumps(
                {
                    "result_contract": "compact-v1",
                    "audit": {
                        "status": "accepted",
                        "action_scope": {
                            "repo": target["repo"],
                            "github_account": publisher,
                            "sources": [{"source_type": target["source_type"], "number": target["number"]}],
                        },
                    },
                },
                sort_keys=True,
            ),
        )
        db_write_github_actions.insert_canary_fixture(
            conn, target["html_url"], now, json.dumps({"body": body, "marker_id": marker_id}, sort_keys=True)
        )
    return body


def _action_state(db_path):
    with closing(db_database.connect(db_path)) as conn:
        row = db_records.first(db_read_github_actions.find_for_action_state(conn))
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
    publisher = None
    if confirm_github_write:
        account = github_client.try_read_json(["gh", "api", "user"], runner=run_command)
        publisher = account.get("login") if isinstance(account, dict) else None
        if not isinstance(publisher, str) or not publisher.strip():
            return {
                "ok": False,
                "status": "publisher_unavailable",
                "safe_error": "Authenticated publisher identity unavailable",
            }
    body = _insert_canary_fixture(db_path, target, marker_id, publisher)
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
