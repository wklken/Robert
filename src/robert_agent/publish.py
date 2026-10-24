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

from robert_agent import (
    audit_result,
    pr_health,
    publication_reconcile,
    remediation,
    retry_policy,
    execution_operations,
    execution_store,
)
from robert_agent.common import emit
from robert_agent.publication_lock import task_publication_lock

PUBLISH_COMMAND_TIMEOUT_SECONDS = 60


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
        **{key: result.get(key) for key in ("reason_code", "error_class", "attempt_count", "max_attempts", "next_retry_at", "required_action", "retryable") if key in result},
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
        existing_metadata = conn.execute("SELECT metadata_json FROM notifications WHERE notification_id=?", (notification_id,)).fetchone()
        previous = _decode_json(existing_metadata[0])
        if previous.get('delivery') and all(previous.get(key) == metadata.get(key) for key in ('reason_code', 'error_class', 'required_action', 'retryable')):
            metadata['delivery'] = previous['delivery']
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


def _publish_comment(action, run_command, conn=None):
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
    scope_guard = _publication_scope_guard(conn, action)
    if scope_guard:
        return scope_guard
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
        if head_owner and not _owners_match(head_owner, existing_owner):
            continue
        dedupe_reason = ""
        if head_owner and not _owners_match(head_owner, existing_owner):
            continue
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
    scope = _trusted_action_scope(conn, action) or {}
    if scope.get("head_owner"):
        return scope["head_owner"]
    if ":" in head:
        return head.split(":", 1)[0]
    owner = _metadata_value(metadata, "head_owner", "fork_owner", "remote_owner", "head_repo_owner")
    if not owner:
        push_metadata = _sibling_push_metadata(conn, action, head)
        owner = _head_owner_from_push_metadata(push_metadata, head, run_command) if push_metadata else ""
    return owner


def _trusted_head_owner_for_intent(conn, action, action_scope, run_command):
    """Resolve the immutable PR head owner without trusting Worker metadata."""

    scoped_owner = (
        action_scope.get("head_owner") if isinstance(action_scope, dict) else ""
    )
    if isinstance(scoped_owner, str) and scoped_owner:
        return scoped_owner
    metadata = _decode_json(action.get("metadata_json"))
    head = _metadata_value(metadata, "head", "head_branch", "branch")
    if ":" in head:
        return head.split(":", 1)[0]
    push_metadata = _sibling_push_metadata(conn, action, head)
    if push_metadata:
        try:
            return _head_owner_from_push_metadata(push_metadata, head, run_command)
        except Exception:
            return ""
    repo = action_scope.get("repo") if isinstance(action_scope, dict) else ""
    return repo.split("/", 1)[0] if isinstance(repo, str) and "/" in repo else ""


def _head_for_pr_create(repo, head, owner):
    if ":" in head:
        return head
    repo_owner = repo.split("/", 1)[0]
    if owner and owner.lower() == repo_owner.lower():
        return head
    return f"{owner}:{head}" if owner else head


def _publish_open_pr(action, run_command, conn=None, trusted_head_owner=None):
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
    head_owner = (
        trusted_head_owner
        if trusted_head_owner is not None
        else _head_owner_for_action(metadata, head, action, conn, run_command)
    )
    existing = _find_existing_open_pr(repo, head, marker, run_command, base=base, head_owner=head_owner)
    if not existing["ok"] or existing["status"] == "published":
        return existing
    scope_guard = _publication_scope_guard(conn, action)
    if scope_guard:
        return scope_guard
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


def _publish_push_existing_pr(action, run_command, conn=None, source_ref="HEAD"):
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
    action_scope = _trusted_action_scope(conn, action) or {}
    expected_remote_sha = action_scope.get("existing_pr_head_sha", "")
    result_head_sha = action_scope.get("result_head_sha") or (source_ref if source_ref != "HEAD" else None)
    source_ref = result_head_sha or source_ref
    destination_ref = f"refs/heads/{branch}"
    if action_scope and not result_head_sha:
        return {"ok": False, "status": "publish_failed",
                "safe_error": "audited result SHA is missing; retry the task to obtain a new audited result"}
    if expected_remote_sha:
        lookup_command = ["git", "ls-remote", "--heads", remote, branch]
        lookup = run_command(lookup_command, cwd=worktree_path, capture_output=True, text=True)
        lookup_error = _command_failure(lookup, "git ls-remote failed")
        if lookup_error:
            return {
                "ok": False,
                "status": "publish_failed",
                "safe_error": f"remote branch lookup failed: {lookup_error}",
                "command": lookup_command,
            }
        lookup_stdout = (getattr(lookup, "stdout", "") or "").strip()
        current_remote_sha = lookup_stdout.split()[0] if lookup_stdout else ""
        if result_head_sha and current_remote_sha == result_head_sha:
            return {"ok": True, "status": "published", "deduplicated": True,
                    "external_id": branch, "target_url": action.get("target_url"),
                    "result_head_sha": result_head_sha,
                    "response": {"head_sha": result_head_sha}, "command": lookup_command}
        if current_remote_sha and current_remote_sha != expected_remote_sha:
            fetch_command = [
                "git", "fetch", "--no-tags", "--no-write-fetch-head",
                remote, destination_ref,
            ]
            fetched = run_command(
                fetch_command,
                cwd=worktree_path,
                capture_output=True,
                text=True,
            )
            fetch_error = _command_failure(fetched, "git fetch failed")
            if fetch_error:
                return {
                    "ok": False,
                    "status": "publish_failed",
                    "safe_error": f"remote branch refresh failed: {fetch_error}",
                    "command": fetch_command,
                }
            ancestor_command = [
                "git", "merge-base", "--is-ancestor", current_remote_sha, result_head_sha
            ]
            ancestor = run_command(
                ancestor_command,
                cwd=worktree_path,
                capture_output=True,
                text=True,
            )
            if ancestor.returncode == 0:
                expected_remote_sha = current_remote_sha
            elif ancestor.returncode == 1:
                return {
                    "ok": True,
                    "status": "superseded",
                    "safe_error": "remote branch diverged from audited result; refusing to overwrite unreviewed head",
                    "command": ancestor_command,
                }
            else:
                safe_error = _command_failure(
                    ancestor,
                    "git merge-base --is-ancestor failed",
                )
                return {
                    "ok": False,
                    "status": "publish_failed",
                    "safe_error": f"remote branch ancestry check failed: {safe_error}",
                    "command": ancestor_command,
                }
        command = [
            "git",
            "push",
            f"--force-with-lease={branch}:{expected_remote_sha}",
            remote,
            f"{source_ref}:{destination_ref}",
        ]
    else:
        command = ["git", "push", remote, f"{source_ref}:{destination_ref}"]
    scope_guard = _publication_scope_guard(conn, action)
    if scope_guard:
        return scope_guard
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


def _trusted_action_scope(conn, action):
    if not conn or not action.get("result_id"):
        return None
    row = conn.execute(
        "SELECT metadata_json FROM worker_results WHERE result_id = ?",
        (action["result_id"],),
    ).fetchone()
    metadata = _decode_json(row[0] if row else None)
    audit = metadata.get("audit")
    if not isinstance(audit, dict) or audit.get("status") != "accepted":
        return None
    return audit.get("action_scope")


def _publish_scope_violation(conn, action):
    action_scope = _trusted_action_scope(conn, action)
    if action_scope is None:
        return None
    payload = _decode_json(action["metadata_json"])
    payload["type"] = action["action_type"]
    if action.get("target_url"):
        payload["target_url"] = action["target_url"]
        payload["url"] = action["target_url"]
    return audit_result.action_scope_violation([payload], action_scope)


def _publication_scope_guard(conn, action):
    """Recheck the durable binding immediately before an external write."""

    if conn is None:
        return None
    scope = _trusted_action_scope(conn, action)
    if scope is None or execution_operations.publication_scope_is_current(
        conn, action.get("task_id"), scope
    ):
        return None
    now = _now()
    intent = _load_intent(conn, action["action_id"])
    reconciliation = {
        "status": "unknown",
        "reason_code": "publication_scope_stale",
        "evidence": {},
    }
    _mark_attention(conn, action, intent, scope, reconciliation, now)
    conn.commit()
    return {
        "ok": False,
        "status": "publish_failed",
        "reason_code": "publication_scope_stale",
        "safe_error": "publication scope is no longer active",
        "command": [],
    }


def _publish_remediation_push(action, run_command, action_scope, conn=None):
    repo = action_scope.get("repo")
    pr_number = action_scope.get("pr_number")
    observed_head_sha = action_scope.get("observed_head_sha")
    observed_base_sha = action_scope.get("observed_base_sha")
    audited_result_head_sha = action_scope.get("result_head_sha")
    if (
        not repo
        or not pr_number
        or not observed_head_sha
        or not observed_base_sha
        or not audited_result_head_sha
    ):
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": "remediation publish scope is incomplete",
        }
    snapshot = pr_health.fetch_pr_snapshot(
        repo,
        pr_number,
        runner=run_command,
    )
    if snapshot is None:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": "remediation PR snapshot revalidation failed",
        }
    if (
        snapshot.get("state") == "open"
        and not snapshot.get("merged")
        and snapshot.get("head_sha") == audited_result_head_sha
        and snapshot.get("base_sha") == observed_base_sha
    ):
        return {"ok": True, "status": "published", "deduplicated": True,
                "external_id": str(pr_number), "target_url": action.get("target_url"),
                "result_head_sha": audited_result_head_sha,
                "response": {"head_sha": audited_result_head_sha}}
    if (
        snapshot.get("state") != "open"
        or snapshot.get("merged")
        or snapshot.get("head_sha") != observed_head_sha
        or snapshot.get("base_sha") != observed_base_sha
    ):
        return {
            "ok": True,
            "status": "superseded",
            "safe_error": "remediation PR snapshot changed before publication",
            "snapshot": snapshot,
        }
    metadata = _decode_json(action["metadata_json"])
    worktree_path = _metadata_value(metadata, "worktree_path")
    head_command = ["git", "rev-parse", "HEAD"]
    completed = run_command(
        head_command,
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    safe_error = _command_failure(completed, "git rev-parse HEAD failed")
    local_head_sha = (getattr(completed, "stdout", "") or "").strip()
    if safe_error or not local_head_sha:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": safe_error or "git rev-parse HEAD returned no SHA",
            "command": head_command,
        }
    if local_head_sha != audited_result_head_sha:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": "remediation worktree HEAD changed after audit",
            "command": head_command,
        }
    result = _publish_push_existing_pr(
        action,
        run_command,
        conn=conn,
        source_ref=audited_result_head_sha,
    )
    if result.get("status") == "published":
        result["result_head_sha"] = audited_result_head_sha
    return result


def _publish_action(
    action,
    run_command,
    conn=None,
    source_sha=None,
    trusted_head_owner=None,
):
    scope_violation = _publish_scope_violation(conn, action)
    if scope_violation:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": f"action scope validation failed: {scope_violation}",
            "retryable": False,
        }
    scope_guard = _publication_scope_guard(conn, action)
    if scope_guard:
        return scope_guard
    if action["action_type"] == "comment":
        return _publish_comment(action, run_command, conn=conn)
    if action["action_type"] == "open_pr":
        return _publish_open_pr(
            action,
            run_command,
            conn=conn,
            trusted_head_owner=trusted_head_owner,
        )
    if action["action_type"] == "push_existing_pr":
        action_scope = _trusted_action_scope(conn, action)
        if (
            isinstance(action_scope, dict)
            and action_scope.get("task_kind")
            in {"ci_remediation", "merge_conflict_remediation"}
        ):
            return _publish_remediation_push(
                action,
                run_command,
                action_scope,
                conn=conn,
            )
        return _publish_push_existing_pr(
            action,
            run_command,
            conn=conn,
            source_ref=source_sha or "HEAD",
        )
    return {
        "ok": False,
        "status": "unsupported_action",
        "safe_error": f"publisher does not support action_type {action['action_type']}",
    }


def _mark_published(conn, action, result, now, intent=None):
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
    if intent:
        current = intent["status"]
        if current in {"executing", "failed"}:
            if current == "failed":
                execution_store.transition_publication_intent(
                    conn, intent["publication_id"], "executing", now
                )
            execution_store.transition_publication_intent(
                conn,
                intent["publication_id"],
                "published",
                now,
                remote_evidence={
                    "status": "confirmed_effect",
                    "reason_code": "publisher_success",
                },
            )


def _derive_push_source_sha(action, action_scope, run_command):
    """Read the exact local source SHA for a non-remediation push."""

    if action.get("action_type") != "push_existing_pr":
        return action_scope, None
    scope = dict(action_scope or {})
    if scope.get("result_head_sha"):
        return scope, scope.get("result_head_sha")
    metadata = _decode_json(action.get("metadata_json"))
    worktree_path = _metadata_value(metadata, "worktree_path") or scope.get("worktree_path")
    source_ref = _metadata_value(metadata, "source_ref") or "HEAD"
    if not worktree_path:
        return scope, None
    command = ["git", "rev-parse", source_ref]
    try:
        completed = run_command(command, cwd=worktree_path, capture_output=True, text=True)
    except Exception:
        return scope, None
    if _command_failure(completed, "git rev-parse failed"):
        return scope, None
    source_sha = (getattr(completed, "stdout", "") or "").strip().splitlines()
    source_sha = source_sha[0].strip() if source_sha else ""
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", source_sha):
        return scope, None
    scope["result_head_sha"] = source_sha
    return scope, source_sha


def _load_intent(conn, action_id):
    row = conn.execute(
        "SELECT * FROM publication_intents WHERE action_id = ?", (action_id,)
    ).fetchone()
    return dict(row) if row else None


def _insert_workflow_attention_notification(conn, action, reason_code, now):
    notification_id = f"notification-workflow-attention-{action['action_id']}"
    metadata = {
        "action_id": action["action_id"],
        "action_type": action["action_type"],
        "reason_code": str(reason_code)[:96],
    }
    conn.execute(
        """
        INSERT INTO notifications(
          notification_id, task_id, notification_type, channel, status,
          created_at, metadata_json
        ) VALUES (?, ?, 'workflow_attention_required', 'local', 'recorded', ?, ?)
        ON CONFLICT(notification_id) DO UPDATE SET
          status = 'recorded', created_at = excluded.created_at,
          metadata_json = excluded.metadata_json
        """,
        (notification_id, action.get("task_id"), now, _encode_json(metadata)),
    )


def _mark_attention(conn, action, intent, scope, reconciliation, now):
    reason_code = reconciliation.get("reason_code", "publication_unknown")
    _record_failure(conn, action, {"status": "publish_failed", "safe_error": f"Publication requires attention: {reason_code}", "retryable": False}, now)
    if intent and intent["status"] in {"prepared", "executing", "failed"}:
        if intent["status"] == "prepared":
            execution_store.transition_publication_intent(
                conn, intent["publication_id"], "executing", now
            )
        elif intent["status"] == "failed":
            execution_store.transition_publication_intent(
                conn, intent["publication_id"], "executing", now
            )
        execution_store.transition_publication_intent(
            conn,
            intent["publication_id"],
            "attention",
            now,
            remote_evidence=reconciliation.get("evidence", {}),
            error={"reason_code": reason_code},
        )
    execution_operations.mark_publication_attention(
        conn, action.get("task_id"), scope, reason_code, now=now
    )
    _insert_workflow_attention_notification(conn, action, reason_code, now)


def _prepare_publication_intent(db_path, action_id, run_command):
    """Prepare and execute an intent in committed local transactions."""

    now = _now()
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        action_row = conn.execute(
            """
            SELECT action_id, result_id, task_id, action_type, target_url, external_id,
                   audit_status, publish_status, created_at, metadata_json
            FROM github_actions WHERE action_id = ?
            """,
            (action_id,),
        ).fetchone()
        if not action_row:
            return {"state": "missing", "action": None, "intent": None, "scope": None}
        action = dict(action_row)
        if action["publish_status"] != "not_published" or action["audit_status"] != "accepted":
            return {"state": "missing", "action": action, "intent": None, "scope": None}
        scope = _trusted_action_scope(conn, action)
        intent = _load_intent(conn, action_id)
    source_sha = None
    head_owner = None
    if intent is None and (scope is None or not isinstance(scope, dict)):
        return {"state": "scope_missing", "action": action, "intent": None, "scope": None}
    if intent is not None and intent["status"] == "prepared" and not isinstance(scope, dict):
        return {"state": "scope_missing", "action": action, "intent": intent, "scope": scope}
    if intent is not None:
        persisted_evidence = _decode_json(intent.get("remote_evidence_json"))
        source_sha = persisted_evidence.get("source_sha") or None
        head_owner = persisted_evidence.get("head_owner") or None
    # Source SHA is read before deriving the fingerprint and before the
    # prepared/executing commits.  On retries only the immutable value from
    # the existing intent is trusted; mutable local HEAD is never re-read.
    if intent is None or intent["status"] == "prepared":
        if isinstance(scope, dict):
            scope, source_sha = _derive_push_source_sha(action, scope, run_command)
            if action.get("action_type") == "open_pr":
                with closing(sqlite3.connect(db_path)) as conn:
                    conn.row_factory = sqlite3.Row
                    head_owner = _trusted_head_owner_for_intent(
                        conn,
                        action,
                        scope,
                        run_command,
                    )
                if head_owner:
                    scope = {**scope, "head_owner": head_owner}
    if (
        action.get("action_type") == "push_existing_pr"
        and isinstance(scope, dict)
        and not scope.get("result_head_sha")
        and not source_sha
    ):
        return {"state": "source_sha_missing", "action": action, "intent": intent, "scope": scope}
    if action.get("action_type") == "open_pr" and not head_owner:
        return {
            "state": "head_owner_missing",
            "action": action,
            "intent": intent,
            "scope": scope,
        }
    if intent is None:
        if scope is None or not isinstance(scope, dict):
            return {"state": "scope_missing", "action": action, "intent": None, "scope": None}
        try:
            idempotency_key, request_fingerprint = publication_reconcile.derive_publication_keys(
                action, scope
            )
        except ValueError:
            return {"state": "scope_missing", "action": action, "intent": None, "scope": None}
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            intent = dict(
                execution_store.prepare_publication_intent(
                    conn,
                    action_id,
                    idempotency_key,
                    request_fingerprint,
                    now,
                )
            )
    # A terminal published intent is replay success.  Backfill the action if
    # a process crashed after intent completion but before the action update.
    if intent["status"] == "published":
        return {"state": "published", "action": action, "intent": intent, "scope": scope}
    if intent["status"] == "attention":
        return {"state": "attention", "action": action, "intent": intent, "scope": scope}

    if intent["status"] == "executing":
        return {
            "state": "reconcile",
            "action": action,
            "intent": intent,
            "scope": scope,
            "source_sha": source_sha,
            "head_owner": head_owner,
        }

    evidence = {}
    if source_sha:
        evidence["source_sha"] = source_sha
    if head_owner:
        evidence["head_owner"] = head_owner
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        latest = _load_intent(conn, action_id)
        if latest["status"] == "prepared":
            latest = dict(
                execution_store.transition_publication_intent(
                    conn,
                    latest["publication_id"],
                    "executing",
                    now,
                    remote_evidence=evidence or None,
                )
            )
        elif latest["status"] == "failed":
            latest = dict(
                execution_store.transition_publication_intent(
                    conn,
                    latest["publication_id"],
                    "executing",
                    now,
                    remote_evidence=evidence or None,
                )
            )
    return {
        "state": "ready",
        "action": action,
        "intent": latest,
        "scope": scope,
        "source_sha": source_sha,
        "head_owner": head_owner,
    }


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


def _skip_pending_result_actions(
    conn,
    *,
    result_id,
    excluding_action_id,
    result,
    now,
):
    rows = conn.execute(
        """
        SELECT action_id, result_id, task_id, action_type, target_url,
               external_id, audit_status, publish_status, created_at,
               metadata_json
        FROM github_actions
        WHERE result_id = ?
          AND action_id != ?
          AND audit_status = 'accepted'
          AND publish_status = 'not_published'
        """,
        (result_id, excluding_action_id),
    ).fetchall()
    for row in rows:
        _mark_skipped(conn, dict(row), result, now)
    return len(rows)


def _record_failure(conn, action, result, now):
    fresh = conn.execute('SELECT metadata_json FROM github_actions WHERE action_id=?',(action['action_id'],)).fetchone()
    metadata = _decode_json(fresh[0] if fresh else action["metadata_json"])
    prior = dict(metadata.get("publish") or {})
    if prior.get('attempt_charged'):
        prior['attempt_count'] = max(0, int(prior.get('attempt_count', 0)) - 1)
    state = retry_policy.failure_state(prior, result.get("safe_error"), now, retryable=result.get("retryable", True))
    from robert_agent import redaction
    safe = redaction.redact_and_truncate(str(result.get("safe_error", "")), 2000)
    result = {**result, "safe_error": safe.get("text", "Sensitive diagnostic omitted; inspect the local log."), "command": []}
    metadata["publish"] = {
        **state,
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
    _insert_notification(conn, action, "github_publish_failed", {**result, **state}, now)
    if not state["retryable"]:
        intent = _load_intent(conn, action["action_id"])
        if intent and intent["status"] == "executing":
            execution_store.transition_publication_intent(conn, intent["publication_id"], "attention", now,
                error={"reason_code": state["reason_code"]})
        execution_operations.mark_publication_attention(conn, action.get("task_id"),
            _trusted_action_scope(conn, action), state["reason_code"], now=now)


def _record_remediation_publish_outcome(conn, action, result, now):
    if action["action_type"] != "push_existing_pr":
        return
    action_scope = _trusted_action_scope(conn, action)
    if not isinstance(action_scope, dict):
        return
    episode_id = action_scope.get("episode_id")
    if (
        action_scope.get("task_kind")
        not in {"ci_remediation", "merge_conflict_remediation"}
        or not episode_id
    ):
        return
    remediation.record_publish_result(
        conn,
        episode_id=episode_id,
        publish_status=result["status"],
        result_head_sha=result.get("result_head_sha"),
        now=now,
    )


def _ready_actions(conn, limit, repo_id=None, task_ids=None):
    repo_filter = ""
    params = [_now()]
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
    if task_ids is not None:
        repo_filter += ' AND github_actions.task_id IN (' + ','.join('?' for _ in task_ids) + ')'
        params.extend(task_ids)
    repo_filter += """ AND NOT EXISTS (SELECT 1 FROM execution_subjects control JOIN tasks own ON own.task_id=github_actions.task_id
        WHERE control.binding_status='attention' AND ((control.subject_type='task' AND control.subject_id=own.task_id)
        OR (control.subject_type='remediation_episode' AND control.subject_id=json_extract(own.metadata_json,'$.remediation_episode_id'))))"""
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT action_id, result_id, task_id, action_type, target_url, external_id,
                   audit_status, publish_status, created_at, metadata_json
            FROM github_actions
            WHERE {retry_policy.publication_ready_sql()}
              {repo_filter}
            ORDER BY created_at, action_id
            LIMIT ?
            """,
            params + [limit],
        )
    ]


def publish_ready_actions(db_path, dry_run=False, limit=20, run_command=subprocess.run, repo_id=None, task_ids=None):
    def bounded_command(*args, **kwargs):
        kwargs["timeout"] = min(kwargs.get("timeout") or PUBLISH_COMMAND_TIMEOUT_SECONDS, PUBLISH_COMMAND_TIMEOUT_SECONDS)
        return run_command(*args, **kwargs)

    with closing(sqlite3.connect(db_path)) as conn:
        execution_store.require_migrated(conn)
        conn.row_factory = sqlite3.Row
        actions = _ready_actions(conn, limit, repo_id=repo_id, task_ids=task_ids)
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
    failed_remediation_result_ids = set()
    busy_task_ids = set()
    for action in actions:
        if action["task_id"] in busy_task_ids:
            continue
        with task_publication_lock(db_path, action["task_id"]) as acquired:
            if not acquired:
                busy_task_ids.add(action["task_id"])
                continue
            # Consume the request before this pass, including reconciliation
            # failures, so a failed attempt never leaves retry disabled.
            with closing(sqlite3.connect(db_path)) as retry_conn, retry_conn:
                retry_conn.execute(
                    """UPDATE wakeups SET status='consumed', updated_at=?
                    WHERE task_id=? AND status='pending'
                    AND json_extract(metadata_json, '$.kind')='publication_retry'""",
                    (_now(), action["task_id"]),
                )
            try:
                result_id = action["result_id"]
                now = _now()
                if result_id and result_id in failed_result_ids:
                    if result_id in failed_remediation_result_ids:
                        result = {
                            "ok": False,
                            "status": "publish_failed",
                            "safe_error": (
                                "skipped because the preceding remediation "
                                "push failed"
                            ),
                        }
                        with closing(sqlite3.connect(db_path)) as conn, conn:
                            conn.row_factory = sqlite3.Row
                            _mark_skipped(conn, action, result, now)
                        skipped_count += 1
                    continue
                current = _prepare_publication_intent(db_path, action["action_id"], bounded_command)
                if current["state"] == "missing":
                    continue
                action = current["action"]
                intent = current["intent"]
                scope = current.get("scope")

                if current["state"] == "published":
                    # A completed intent is replay success.  Normally the action was
                    # committed in the same transaction; this branch repairs an old
                    # partial local update without contacting GitHub again.
                    if action.get("publish_status") == "not_published":
                        result = {
                            "ok": True,
                            "status": "published",
                            "deduplicated": True,
                            "response": {"replayed_intent": True},
                        }
                        with closing(sqlite3.connect(db_path)) as conn, conn:
                            conn.row_factory = sqlite3.Row
                            _mark_published(conn, action, result, now)
                    published_count += 1
                    deduplicated_count += 1
                    continue

                if current["state"] == "attention":
                    failed_count += 1
                    failures.append(
                        {
                            "action_id": action["action_id"],
                            "safe_error": "publication intent requires operator attention",
                            "command": [],
                        }
                    )
                    failed_result_ids.add(result_id) if result_id else None
                    continue

                if current["state"] in {
                    "scope_missing",
                    "source_sha_missing",
                    "head_owner_missing",
                }:
                    # There is no safe idempotency key without the accepted audit
                    # scope.  Protect the action with one deterministic local
                    # notification and stop before any remote query or write.
                    with closing(sqlite3.connect(db_path)) as conn, conn:
                        conn.row_factory = sqlite3.Row
                        reason_code = (
                            "audited_scope_missing"
                            if current["state"] == "scope_missing"
                            else (
                                "source_sha_unavailable"
                                if current["state"] == "source_sha_missing"
                                else "head_owner_unavailable"
                            )
                        )
                        _record_failure(conn, action, {"status": "publish_failed", "safe_error": f"Publication requires attention: {reason_code}", "retryable": False}, now)
                        execution_operations.mark_publication_attention(
                            conn,
                            action.get("task_id"),
                            scope,
                            reason_code,
                            now=now,
                        )
                        _insert_workflow_attention_notification(
                            conn,
                            action,
                            reason_code,
                            now,
                        )
                    failed_count += 1
                    failures.append(
                        {
                            "action_id": action["action_id"],
                            "safe_error": (
                                "publication reconciliation: audited_scope_missing"
                                if current["state"] == "scope_missing"
                                else (
                                    "publication reconciliation: source_sha_unavailable"
                                    if current["state"] == "source_sha_missing"
                                    else "publication reconciliation: head_owner_unavailable"
                                )
                            ),
                            "command": [],
                        }
                    )
                    if result_id:
                        failed_result_ids.add(result_id)
                    continue

                if current["state"] == "reconcile":
                    reconcile_scope = dict(scope or {})
                    evidence = _decode_json(intent.get("remote_evidence_json"))
                    if evidence.get("source_sha") and not reconcile_scope.get("result_head_sha"):
                        reconcile_scope["result_head_sha"] = evidence["source_sha"]
                    if evidence.get("head_owner"):
                        reconcile_scope["head_owner"] = evidence["head_owner"]
                    reconciliation = publication_reconcile.classify_remote_state(
                        action,
                        reconcile_scope if scope is not None else None,
                        bounded_command,
                    )
                    if reconciliation["status"] == "confirmed_effect":
                        result = {
                            "ok": True,
                            "status": "published",
                            "deduplicated": True,
                            "external_id": reconciliation["evidence"].get("external_id", ""),
                            "target_url": reconciliation["evidence"].get("target_url", ""),
                            "response": {"reconciliation": reconciliation["evidence"]},
                        }
                        with closing(sqlite3.connect(db_path)) as conn, conn:
                            conn.row_factory = sqlite3.Row
                            latest = _load_intent(conn, action["action_id"])
                            _mark_published(conn, action, result, now, intent=latest)
                            _record_remediation_publish_outcome(conn, action, result, now)
                        published_count += 1
                        deduplicated_count += 1
                        continue
                    if reconciliation["status"] == "unknown":
                        current = {**current, "state": "attention"}
                    if reconciliation["status"] == "confirmed_absent" and scope is not None:
                        with closing(sqlite3.connect(db_path)) as conn, conn:
                            conn.row_factory = sqlite3.Row
                            if not execution_operations.publication_scope_is_current(
                                conn, action.get("task_id"), scope
                            ):
                                reconciliation = {
                                    "status": "unknown",
                                    "reason_code": "publication_scope_stale",
                                    "evidence": {},
                                }
                            else:
                                latest = _load_intent(conn, action["action_id"])
                                persisted = _decode_json(latest.get("remote_evidence_json"))
                                source_sha = persisted.get("source_sha") or current.get(
                                    "source_sha"
                                )
                                head_owner = persisted.get("head_owner") or current.get(
                                    "head_owner"
                                )
                                absent_evidence = dict(reconciliation.get("evidence", {}))
                                if source_sha:
                                    absent_evidence["source_sha"] = source_sha
                                if head_owner:
                                    absent_evidence["head_owner"] = head_owner
                                execution_store.transition_publication_intent(
                                    conn,
                                    latest["publication_id"],
                                    "confirmed_absent",
                                    now,
                                    remote_evidence=absent_evidence,
                                )
                                retry_evidence = {"retry_after_absence": True}
                                if source_sha:
                                    retry_evidence["source_sha"] = source_sha
                                if head_owner:
                                    retry_evidence["head_owner"] = head_owner
                                execution_store.transition_publication_intent(
                                    conn,
                                    latest["publication_id"],
                                    "executing",
                                    now,
                                    remote_evidence=retry_evidence,
                                )
                        if reconciliation["status"] == "confirmed_absent":
                            current = {**current, "state": "ready", "intent": latest}
                        else:
                            current = {**current, "state": "attention"}
                    if current["state"] == "attention":
                        with closing(sqlite3.connect(db_path)) as conn, conn:
                            conn.row_factory = sqlite3.Row
                            latest = _load_intent(conn, action["action_id"])
                            _mark_attention(conn, action, latest, scope, reconciliation, now)
                        failed_count += 1
                        failures.append(
                            {
                                "action_id": action["action_id"],
                                "safe_error": f"publication reconciliation: {reconciliation.get('reason_code', 'unknown')}",
                                "command": [],
                            }
                        )
                        if result_id:
                            failed_result_ids.add(result_id)
                        continue

                if current.get("state") == "ready" and scope is None:
                    reconciliation = {
                        "status": "unknown",
                        "reason_code": "audited_scope_missing",
                        "evidence": {},
                    }
                    with closing(sqlite3.connect(db_path)) as conn, conn:
                        conn.row_factory = sqlite3.Row
                        latest = _load_intent(conn, action["action_id"])
                        _mark_attention(conn, action, latest, scope, reconciliation, now)
                    failed_count += 1
                    failures.append(
                        {
                            "action_id": action["action_id"],
                            "safe_error": "publication reconciliation: audited_scope_missing",
                            "command": [],
                        }
                    )
                    if result_id:
                        failed_result_ids.add(result_id)
                    continue

                # Charge the durable budget before remote I/O, including a process
                # crash that never reaches failure recording.
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.row_factory = sqlite3.Row
                    fresh = conn.execute('SELECT metadata_json FROM github_actions WHERE action_id=?',(action['action_id'],)).fetchone()
                    metadata = _decode_json(fresh[0])
                    prior = metadata.get('publish') or {}
                    first = prior.get('first_failed_at') or now
                    expired = (retry_policy.timestamp(now) - retry_policy.timestamp(first)).total_seconds() >= retry_policy.MAX_ELAPSED_SECONDS
                    if int(prior.get('attempt_count',0)) >= retry_policy.MAX_ATTEMPTS or expired:
                        _record_failure(conn, action, {'safe_error':'Automatic publication retry budget exhausted. Inspect and retry explicitly.', 'retryable':False}, now)
                        failed_count += 1
                        if result_id:
                            failed_result_ids.add(result_id)
                        continue
                    metadata['publish'] = {**prior, 'attempt_count':int(prior.get('attempt_count',0))+1,
                        'first_failed_at':first, 'attempt_charged':True, 'last_attempt_at':now}
                    conn.execute('UPDATE github_actions SET metadata_json=? WHERE action_id=?',(_encode_json(metadata), action['action_id']))
                    action = {**action,'metadata_json':_encode_json(metadata)}
                # The intent is committed as executing before this call.  No SQLite
                # transaction spans the remote effect, so a crash leaves an executing
                # intent for the next publisher pass to reconcile.
                with closing(sqlite3.connect(db_path)) as conn:
                    conn.row_factory = sqlite3.Row
                    result = _publish_action(
                        action,
                        bounded_command,
                        conn=conn,
                        source_sha=current.get("source_sha"),
                        trusted_head_owner=current.get("head_owner"),
                    )
                now = _now()
                if result["status"] == "published":
                    with closing(sqlite3.connect(db_path)) as conn, conn:
                        conn.row_factory = sqlite3.Row
                        latest = _load_intent(conn, action["action_id"])
                        _mark_published(conn, action, result, now, intent=latest)
                        _record_remediation_publish_outcome(conn, action, result, now)
                    published_count += 1
                    if result.get("deduplicated"):
                        deduplicated_count += 1
                    continue
                if result["status"] in {"unsupported_action", "superseded"}:
                    with closing(sqlite3.connect(db_path)) as conn, conn:
                        conn.row_factory = sqlite3.Row
                        _record_remediation_publish_outcome(conn, action, result, now)
                        _mark_skipped(conn, action, result, now)
                        skipped_count += 1
                        if result["status"] == "superseded" and result_id:
                            skipped_count += _skip_pending_result_actions(
                                conn,
                                result_id=result_id,
                                excluding_action_id=action["action_id"],
                                result={
                                    "ok": True,
                                    "status": "superseded",
                                    "safe_error": (
                                        "skipped because the preceding push was superseded"
                                    ),
                                },
                                now=now,
                            )
                    continue
                # A non-zero/raised remote command is ambiguous.  Keep the intent in
                # executing and let the next pass reconcile before any retry.
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.row_factory = sqlite3.Row
                    _record_remediation_publish_outcome(conn, action, result, now)
                    _record_failure(conn, action, result, now)
                if result_id:
                    failed_result_ids.add(result_id)
                    action_scope = scope
                    if (
                        action["action_type"] == "push_existing_pr"
                        and isinstance(action_scope, dict)
                        and action_scope.get("task_kind")
                        in {"ci_remediation", "merge_conflict_remediation"}
                    ):
                        failed_remediation_result_ids.add(result_id)
                failed_count += 1
                failures.append(
                    {
                        "action_id": action["action_id"],
                        "safe_error": result.get("safe_error", ""),
                        "command": result.get("command", []),
                    }
                )
            except subprocess.TimeoutExpired:
                result = {"ok": False, "status": "publish_failed",
                          "safe_error": "Publication command timed out after 60 seconds. Check connectivity and retry publication."}
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.row_factory = sqlite3.Row
                    _record_failure(conn, action, result, _now())
                failed_count += 1
                failures.append({"action_id": action["action_id"], "safe_error": result["safe_error"], "command": []})
                if action["result_id"]:
                    failed_result_ids.add(action["result_id"])
            except OSError as exc:
                result = {'status':'publish_failed','safe_error':type(exc).__name__, 'retryable': not isinstance(exc, FileNotFoundError)}
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.row_factory = sqlite3.Row
                    _record_failure(conn, action, result, _now())
                failed_count += 1
                failures.append({'action_id':action['action_id'],'safe_error':result['safe_error'],'command':[]})
                if action['result_id']:
                    failed_result_ids.add(action['result_id'])
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
