#!/usr/bin/env python3
import argparse
from contextlib import closing
import json
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import uuid4

from robert_agent.common import emit


COMMENT_MARKER_RE = re.compile(
    r"<!--\s*(?:robert-comment|dd-comment)\b.*?-->",
    re.I | re.S,
)
WORKSTREAM_MARKER_RE = re.compile(
    r"<!--\s*(?:robert-workstream|dd-workstream)\b.*?-->",
    re.I | re.S,
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _decode_json(value):
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def _encode_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _id(prefix):
    return f"{prefix}-{uuid4().hex[:12]}"


def _notification_metadata(action, result):
    return {
        "action_id": action["action_id"],
        "action_type": action["action_type"],
        "safe_error": result.get("safe_error", ""),
        "command": result.get("command", []),
    }


def _existing_action_notification(conn, action, notification_type):
    for row in conn.execute(
        """
        SELECT notification_id, metadata_json
        FROM notifications
        WHERE task_id = ?
          AND notification_type = ?
          AND channel = 'local'
        ORDER BY created_at DESC, notification_id DESC
        """,
        (action["task_id"], notification_type),
    ):
        metadata = _decode_json(row["metadata_json"])
        if metadata.get("action_id") == action["action_id"]:
            return row["notification_id"]
    return None


def _insert_notification(conn, action, notification_type, result, now):
    metadata = _notification_metadata(action, result)
    notification_id = _existing_action_notification(conn, action, notification_type)
    if notification_id:
        conn.execute(
            """
            UPDATE notifications
            SET status = 'recorded',
                created_at = ?,
                metadata_json = ?
            WHERE notification_id = ?
            """,
            (now, _encode_json(metadata), notification_id),
        )
        return
    conn.execute(
        """
        INSERT INTO notifications(
          notification_id, task_id, notification_type, channel, status, created_at, metadata_json
        )
        VALUES (?, ?, ?, 'local', 'recorded', ?, ?)
        """,
        (
            _id("notification"),
            action["task_id"],
            notification_type,
            now,
            _encode_json(metadata),
        ),
    )


def _resolve_action_notification(conn, action, notification_type, result, now):
    notification_id = _existing_action_notification(conn, action, notification_type)
    if not notification_id:
        return
    row = conn.execute(
        "SELECT metadata_json FROM notifications WHERE notification_id = ?",
        (notification_id,),
    ).fetchone()
    metadata = _decode_json(row[0] if row else "{}")
    metadata["resolved_at"] = now
    metadata["resolved_publish_status"] = result.get("status", "")
    metadata["resolved_external_id"] = result.get("external_id", "")
    metadata["resolved_target_url"] = result.get("target_url", "")
    conn.execute(
        """
        UPDATE notifications
        SET status = 'resolved',
            created_at = ?,
            metadata_json = ?
        WHERE notification_id = ?
        """,
        (now, _encode_json(metadata), notification_id),
    )


def _issue_comment_endpoint(target_url):
    parsed = urlparse(target_url or "")
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc != "github.com" or len(parts) < 4:
        return None
    owner, repo, source_type, number = parts[:4]
    if source_type not in {"issues", "pull"} or not number.isdigit():
        return None
    return f"repos/{owner}/{repo}/issues/{number}/comments"


def _hidden_marker(body, marker_re):
    match = marker_re.search(body or "")
    return match.group(0) if match else ""


def _iter_json_objects(value):
    if isinstance(value, dict):
        yield value
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_json_objects(item)


def _command_failure(completed, fallback):
    if getattr(completed, "returncode", 0) == 0:
        return None
    return (getattr(completed, "stderr", "") or fallback).strip()


def _find_existing_comment(endpoint, marker, run_command):
    command = ["gh", "api", f"{endpoint}?per_page=100", "--paginate", "--slurp"]
    completed = run_command(command, capture_output=True, text=True)
    safe_error = _command_failure(completed, "gh api comment lookup failed")
    if safe_error:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": f"comment duplicate preflight failed: {safe_error}",
            "command": command,
        }
    response = _decode_json(getattr(completed, "stdout", ""))
    for comment in _iter_json_objects(response):
        body = comment.get("body") if isinstance(comment.get("body"), str) else ""
        if marker and marker in body:
            return {
                "ok": True,
                "status": "published",
                "deduplicated": True,
                "external_id": str(comment.get("id") or ""),
                "target_url": comment.get("html_url") or comment.get("url") or "",
                "response": {"existing_comment": comment},
                "command": command,
            }
    return {"ok": True, "status": "not_found", "command": command}


def _publish_comment(action, run_command):
    metadata = _decode_json(action["metadata_json"])
    body = metadata.get("body")
    if not isinstance(body, str) or not body:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": "comment action requires body",
        }
    marker = _hidden_marker(body, COMMENT_MARKER_RE)
    if not marker:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": "comment action requires robert-comment idempotency marker",
        }
    endpoint = _issue_comment_endpoint(action["target_url"] or metadata.get("target_url") or metadata.get("url"))
    if not endpoint:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": "comment action requires GitHub issue or PR target_url",
        }
    existing = _find_existing_comment(endpoint, marker, run_command)
    if not existing["ok"] or existing["status"] == "published":
        return existing
    command = ["gh", "api", endpoint, "-f", f"body={body}"]
    completed = run_command(command, capture_output=True, text=True)
    safe_error = _command_failure(completed, "gh api failed")
    if safe_error:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": safe_error,
            "command": command,
        }
    response = _decode_json(getattr(completed, "stdout", ""))
    if not isinstance(response, dict):
        response = {}
    return {
        "ok": True,
        "status": "published",
        "external_id": str(response.get("id") or ""),
        "target_url": response.get("html_url") or response.get("url") or action["target_url"],
        "response": response,
        "command": command,
    }


def _pr_number_from_url(url):
    match = re.search(r"/pull/(\d+)(?:$|[/?#])", url or "")
    return match.group(1) if match else ""


def _metadata_value(metadata, *keys):
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _head_branch(head):
    if ":" in head:
        return head.split(":", 1)[1]
    return head


def _owner_login(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("login", "name"):
            owner = value.get(key)
            if isinstance(owner, str) and owner:
                return owner
        owner = value.get("owner")
        if owner is not value:
            return _owner_login(owner)
    return ""


def _owners_match(expected, actual):
    return bool(expected and actual and expected.lower() == actual.lower())


def _pr_head_owner(pr):
    return _owner_login(pr.get("headRepositoryOwner")) or _owner_login(pr.get("headRepository"))


def _find_existing_open_pr(repo, head, marker, run_command, base="", head_owner=""):
    branch = _head_branch(head)
    command = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--head",
        branch,
        "--state",
        "open",
        "--json",
        "number,url,baseRefName,headRefName,headRepositoryOwner",
    ]
    completed = run_command(command, capture_output=True, text=True)
    safe_error = _command_failure(completed, "gh pr list failed")
    if safe_error:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": f"open_pr duplicate preflight failed: {safe_error}",
            "command": command,
        }
    response = _decode_json(getattr(completed, "stdout", ""))
    for pr in _iter_json_objects(response):
        url = pr.get("url") or ""
        number = str(pr.get("number") or _pr_number_from_url(url) or "")
        base_ref = pr.get("baseRefName") or ""
        if base and base_ref and base_ref != base:
            continue
        head_ref = pr.get("headRefName") or ""
        if branch and head_ref and head_ref != branch:
            continue
        target = number or url
        if not target:
            continue
        view_command = [
            "gh",
            "pr",
            "view",
            target,
            "--repo",
            repo,
            "--json",
            "number,url,body,baseRefName,headRefName,headRepositoryOwner",
        ]
        view = run_command(view_command, capture_output=True, text=True)
        safe_error = _command_failure(view, "gh pr view failed")
        if safe_error:
            return {
                "ok": False,
                "status": "publish_failed",
                "safe_error": f"open_pr duplicate preflight failed: {safe_error}",
                "command": view_command,
            }
        existing = _decode_json(getattr(view, "stdout", ""))
        existing_base = existing.get("baseRefName") or base_ref
        if base and existing_base and existing_base != base:
            continue
        existing_head = existing.get("headRefName") or head_ref
        if branch and existing_head and existing_head != branch:
            continue
        body = existing.get("body") if isinstance(existing.get("body"), str) else ""
        existing_owner = _pr_head_owner(existing) or _pr_head_owner(pr)
        dedupe_reason = ""
        if marker and marker in body:
            dedupe_reason = "matching_marker"
        elif _owners_match(head_owner, existing_owner):
            dedupe_reason = "matching_head"
        else:
            continue
        return {
            "ok": True,
            "status": "published",
            "deduplicated": True,
            "external_id": str(existing.get("number") or number),
            "target_url": existing.get("url") or url,
            "response": {
                "existing_pr": existing or pr,
                "dedupe_reason": dedupe_reason,
            },
            "command": view_command,
        }
    return {"ok": True, "status": "not_found", "command": command}


def _github_remote_owner(remote_url):
    remote_url = (remote_url or "").strip()
    if not remote_url:
        return ""
    if remote_url.startswith("git@") and ":" in remote_url:
        path = remote_url.split(":", 1)[1]
    else:
        path = urlparse(remote_url).path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    return parts[0] if len(parts) >= 2 else ""


def _head_owner_from_push_metadata(metadata, head, run_command):
    branch = _metadata_value(metadata, "branch", "head", "head_branch")
    if branch and branch != head:
        return ""
    worktree_path = _metadata_value(metadata, "worktree_path")
    if not worktree_path:
        return ""
    remote = _metadata_value(metadata, "remote") or "origin"
    completed = run_command(
        ["git", "remote", "get-url", remote],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if _command_failure(completed, "git remote get-url failed"):
        return ""
    return _github_remote_owner(getattr(completed, "stdout", ""))


def _sibling_push_metadata(conn, action, head):
    if not conn or not action["result_id"]:
        return {}
    rows = conn.execute(
        """
        SELECT metadata_json
        FROM github_actions
        WHERE result_id = ?
          AND action_type = 'push_existing_pr'
        ORDER BY created_at DESC, action_id DESC
        """,
        (action["result_id"],),
    ).fetchall()
    for row in rows:
        metadata = _decode_json(row["metadata_json"])
        branch = _metadata_value(metadata, "branch", "head", "head_branch")
        if not branch or branch == head:
            return metadata
    return {}


def _head_owner_for_action(metadata, head, action, conn, run_command):
    if ":" in head:
        return head.split(":", 1)[0]
    owner = _metadata_value(metadata, "head_owner", "fork_owner", "remote_owner", "head_repo_owner")
    if not owner:
        push_metadata = _sibling_push_metadata(conn, action, head)
        owner = _head_owner_from_push_metadata(push_metadata, head, run_command) if push_metadata else ""
    return owner


def _head_for_pr_create(repo, head, owner):
    if ":" in head:
        return head
    repo_owner = repo.split("/", 1)[0]
    if owner and owner.lower() == repo_owner.lower():
        return head
    return f"{owner}:{head}" if owner else head


def _publish_open_pr(action, run_command, conn=None):
    metadata = _decode_json(action["metadata_json"])
    repo = _metadata_value(metadata, "repo", "repository")
    head = _metadata_value(metadata, "head", "head_branch", "branch")
    base = _metadata_value(metadata, "base", "base_branch", "target_base_branch")
    title = _metadata_value(metadata, "title")
    body = _metadata_value(metadata, "body", "pr_body")
    missing = [
        name
        for name, value in {
            "repo": repo,
            "head": head,
            "base": base,
            "title": title,
            "body": body,
        }.items()
        if not value
    ]
    if missing:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": f"open_pr action missing fields: {missing}",
        }
    marker = _hidden_marker(body, WORKSTREAM_MARKER_RE)
    if not marker:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": "open_pr action body requires robert-workstream metadata",
        }
    head_owner = _head_owner_for_action(metadata, head, action, conn, run_command)
    existing = _find_existing_open_pr(repo, head, marker, run_command, base=base, head_owner=head_owner)
    if not existing["ok"] or existing["status"] == "published":
        return existing
    create_head = _head_for_pr_create(repo, head, head_owner)
    command = [
        "gh",
        "pr",
        "create",
        "--repo",
        repo,
        "--head",
        create_head,
        "--base",
        base,
        "--title",
        title,
        "--body",
        body,
    ]
    if metadata.get("draft"):
        command.append("--draft")
    completed = run_command(command, capture_output=True, text=True)
    safe_error = _command_failure(completed, "gh pr create failed")
    if safe_error:
        existing_number = _pr_number_from_url(safe_error)
        if existing_number and "already exists" in safe_error:
            return {
                "ok": True,
                "status": "published",
                "deduplicated": True,
                "external_id": existing_number,
                "target_url": f"https://github.com/{repo}/pull/{existing_number}",
                "response": {"dedupe_reason": "create_reported_existing_pr"},
                "command": command,
            }
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": safe_error,
            "command": command,
        }
    lines = (getattr(completed, "stdout", "") or "").strip().splitlines()
    url = lines[-1].strip() if lines else ""
    return {
        "ok": True,
        "status": "published",
        "external_id": _pr_number_from_url(url),
        "target_url": url or action["target_url"],
        "response": {"url": url},
        "command": command,
    }


def _publish_push_existing_pr(action, run_command):
    metadata = _decode_json(action["metadata_json"])
    worktree_path = _metadata_value(metadata, "worktree_path")
    remote = _metadata_value(metadata, "remote") or "origin"
    branch = _metadata_value(metadata, "branch", "head", "head_branch")
    missing = [
        name
        for name, value in {
            "worktree_path": worktree_path,
            "branch": branch,
        }.items()
        if not value
    ]
    if missing:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": f"push_existing_pr action missing fields: {missing}",
        }
    command = ["git", "push", remote, f"HEAD:{branch}"]
    completed = run_command(command, cwd=worktree_path, capture_output=True, text=True)
    safe_error = _command_failure(completed, "git push failed")
    if safe_error:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": safe_error,
            "command": command,
        }
    return {
        "ok": True,
        "status": "published",
        "external_id": branch,
        "target_url": action["target_url"] or metadata.get("target_url") or metadata.get("url"),
        "response": {"stdout": getattr(completed, "stdout", "")},
        "command": command,
    }


def _publish_action(action, run_command, conn=None):
    if action["action_type"] == "comment":
        return _publish_comment(action, run_command)
    if action["action_type"] == "open_pr":
        return _publish_open_pr(action, run_command, conn=conn)
    if action["action_type"] == "push_existing_pr":
        return _publish_push_existing_pr(action, run_command)
    return {
        "ok": False,
        "status": "unsupported_action",
        "safe_error": f"publisher does not support action_type {action['action_type']}",
    }


def _mark_published(conn, action, result, now):
    metadata = _decode_json(action["metadata_json"])
    metadata["publish"] = {
        "status": "published",
        "published_at": now,
        "response": result.get("response", {}),
        "deduplicated": bool(result.get("deduplicated")),
    }
    conn.execute(
        """
        UPDATE github_actions
        SET publish_status = 'published',
            external_id = COALESCE(NULLIF(?, ''), external_id),
            target_url = COALESCE(NULLIF(?, ''), target_url),
            metadata_json = ?
        WHERE action_id = ?
        """,
        (
            result.get("external_id", ""),
            result.get("target_url"),
            _encode_json(metadata),
            action["action_id"],
        ),
    )
    _resolve_action_notification(conn, action, "github_publish_failed", result, now)


def _mark_skipped(conn, action, result, now):
    metadata = _decode_json(action["metadata_json"])
    metadata["publish"] = {
        "status": "skipped",
        "skipped_at": now,
        "safe_error": result.get("safe_error", ""),
    }
    conn.execute(
        """
        UPDATE github_actions
        SET publish_status = 'skipped',
            metadata_json = ?
        WHERE action_id = ?
        """,
        (_encode_json(metadata), action["action_id"]),
    )
    _insert_notification(conn, action, "github_publish_skipped", result, now)


def _record_failure(conn, action, result, now):
    metadata = _decode_json(action["metadata_json"])
    metadata["publish"] = {
        "status": "publish_failed",
        "failed_at": now,
        "safe_error": result.get("safe_error", ""),
        "command": result.get("command", []),
    }
    conn.execute(
        """
        UPDATE github_actions
        SET metadata_json = ?
        WHERE action_id = ?
        """,
        (_encode_json(metadata), action["action_id"]),
    )
    _insert_notification(conn, action, "github_publish_failed", result, now)


def _ready_actions(conn, limit, repo_id=None):
    repo_filter = ""
    params = []
    if repo_id:
        repo_filter = """
          AND EXISTS (
            SELECT 1
            FROM tasks t
            JOIN workstreams w ON w.workstream_id = t.workstream_id
            WHERE t.task_id = github_actions.task_id
              AND w.repo_id = ?
          )
        """
        params.append(repo_id)
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT action_id, result_id, task_id, action_type, target_url, external_id,
                   audit_status, publish_status, created_at, metadata_json
            FROM github_actions
            WHERE audit_status = 'accepted'
              AND publish_status = 'not_published'
              {repo_filter}
            ORDER BY created_at, action_id
            LIMIT ?
            """,
            params + [limit],
        )
    ]


def publish_ready_actions(db_path, dry_run=False, limit=20, run_command=subprocess.run, repo_id=None):
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        actions = _ready_actions(conn, limit, repo_id=repo_id)
        if dry_run:
            return {
                "ok": True,
                "status": "dry_run",
                "pending_count": len(actions),
                "published_count": 0,
                "deduplicated_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
            }
        published_count = 0
        deduplicated_count = 0
        skipped_count = 0
        failed_count = 0
        failures = []
        failed_result_ids = set()
        for action in actions:
            result_id = action["result_id"]
            if result_id and result_id in failed_result_ids:
                continue
            now = _now()
            result = _publish_action(action, run_command, conn=conn)
            if result["status"] == "published":
                _mark_published(conn, action, result, now)
                published_count += 1
                if result.get("deduplicated"):
                    deduplicated_count += 1
                continue
            if result["status"] == "unsupported_action":
                _mark_skipped(conn, action, result, now)
                skipped_count += 1
                continue
            _record_failure(conn, action, result, now)
            if result_id:
                failed_result_ids.add(result_id)
            failed_count += 1
            failures.append(
                {
                    "action_id": action["action_id"],
                    "safe_error": result.get("safe_error", ""),
                    "command": result.get("command", []),
                }
            )
    return {
        "ok": failed_count == 0,
        "status": "published" if failed_count == 0 else "publish_failed",
        "pending_count": len(actions),
        "published_count": published_count,
        "deduplicated_count": deduplicated_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "failures": failures,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    result = publish_ready_actions(args.db, dry_run=args.dry_run, limit=args.limit)
    return emit(result, 0 if result.get("ok") else 3)


if __name__ == "__main__":
    raise SystemExit(main())
