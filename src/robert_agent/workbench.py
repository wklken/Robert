"""Scenario-specific SQLite read models for the DD GitHub workbench."""

import base64
import binascii
from collections import Counter, defaultdict
from contextlib import closing
import json
import shlex
import sqlite3


ACTIVE_TASK_LIFECYCLES = {"detected", "authorized", "classified", "queued", "running"}
BUCKETS = {"needs_attention", "working", "waiting", "history"}
BUCKET_ORDER = {"needs_attention": 0, "working": 1, "waiting": 2, "history": 3}
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
SORTS = {"priority", "updated-desc", "updated-asc"}
REQUIRED_TABLES = {
    "repos",
    "github_sources",
    "workstreams",
    "tasks",
    "attempts",
    "worker_phases",
    "worker_results",
    "github_actions",
    "artifacts",
    "notifications",
}


class WorkItemQueryError(ValueError):
    """Raised for invalid workbench filters and cursors."""


def _decode_json(value, default=None):
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing = REQUIRED_TABLES - tables
    if missing:
        conn.close()
        raise sqlite3.OperationalError(
            "workbench schema is missing: " + ", ".join(sorted(missing))
        )
    return conn


def _encode_cursor(updated_at, workstream_id):
    raw = json.dumps([updated_at, workstream_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value):
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
        updated_at, workstream_id = json.loads(decoded)
    except (
        ValueError,
        TypeError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        raise WorkItemQueryError("invalid cursor") from exc
    if not isinstance(updated_at, str) or not isinstance(workstream_id, str):
        raise WorkItemQueryError("invalid cursor")
    return updated_at, workstream_id


def _group_rows(conn, sql, key):
    grouped = defaultdict(list)
    for row in conn.execute(sql):
        item = dict(row)
        grouped[item[key]].append(item)
    return grouped


def _load_state(conn):
    workstreams = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
              w.workstream_id,
              w.repo_id,
              w.primary_source_id,
              w.origin_workstream_id,
              w.lifecycle AS workstream_lifecycle,
              w.active_task_id,
              w.created_at,
              w.updated_at,
              r.full_name AS repo_full_name,
              gs.source_key,
              gs.source_type,
              gs.number AS source_number,
              gs.html_url AS github_url,
              gs.title AS source_title,
              gs.state AS source_state,
              gs.author_login,
              gs.source_updated_at
            FROM workstreams w
            JOIN repos r ON r.repo_id = w.repo_id
            LEFT JOIN github_sources gs ON gs.source_id = w.primary_source_id
            """
        )
    ]
    tasks = _group_rows(
        conn,
        """
        SELECT
          task_id, workstream_id, lifecycle, parent_task_id, priority,
          route_id, expected_output, created_at, updated_at
        FROM tasks
        ORDER BY updated_at DESC, task_id DESC
        """,
        "workstream_id",
    )
    attempts = _group_rows(
        conn,
        """
        SELECT
          attempt_id, task_id, attempt_no, status, worktree_path, branch_name,
          started_at, heartbeat_at, finished_at, failure_json
        FROM attempts
        ORDER BY attempt_no DESC, started_at DESC, attempt_id DESC
        """,
        "task_id",
    )
    phases = _group_rows(
        conn,
        """
        SELECT phase_id, attempt_id, phase, status, summary, next_step, created_at
        FROM worker_phases
        ORDER BY created_at, phase_id
        """,
        "attempt_id",
    )
    results = _group_rows(
        conn,
        """
        SELECT
          result_id, task_id, attempt_id, output_type, verification_json,
          handoff, created_at, metadata_json
        FROM worker_results
        ORDER BY created_at DESC, result_id DESC
        """,
        "task_id",
    )
    actions = _group_rows(
        conn,
        """
        SELECT
          action_id, result_id, task_id, action_type, target_url, external_id,
          audit_status, publish_status, created_at, metadata_json
        FROM github_actions
        ORDER BY created_at DESC, action_id DESC
        """,
        "task_id",
    )
    artifacts = _group_rows(
        conn,
        """
        SELECT artifact_id, task_id, attempt_id, artifact_type, path, bytes, created_at
        FROM artifacts
        ORDER BY created_at DESC, artifact_id DESC
        """,
        "task_id",
    )
    notifications = _group_rows(
        conn,
        """
        SELECT notification_id, task_id, notification_type, status, created_at
        FROM notifications
        ORDER BY created_at DESC, notification_id DESC
        """,
        "task_id",
    )
    return {
        "workstreams": workstreams,
        "tasks": tasks,
        "attempts": attempts,
        "phases": phases,
        "results": results,
        "actions": actions,
        "artifacts": artifacts,
        "notifications": notifications,
    }


def _current_task(workstream, tasks):
    active_task_id = workstream.get("active_task_id")
    if active_task_id:
        for task in tasks:
            if task["task_id"] == active_task_id:
                return task
    return tasks[0] if tasks else None


def _publish_error(action):
    metadata = _decode_json(action.get("metadata_json"), {}) or {}
    publish = metadata.get("publish") if isinstance(metadata, dict) else None
    if not isinstance(publish, dict):
        return None
    return publish.get("safe_error") or publish.get("error")


def _derive_operator_state(workstream, task, attempt, results, actions):
    for action in actions:
        if action["publish_status"] == "published":
            continue
        metadata = _decode_json(action.get("metadata_json"), {}) or {}
        publish = metadata.get("publish") if isinstance(metadata, dict) else None
        if isinstance(publish, dict) and publish.get("status") == "publish_failed":
            return (
                "needs_attention",
                "publish_failed",
                "Publishing the accepted GitHub action failed.",
            )
        if action["audit_status"] == "accepted" and action["publish_status"] == "skipped":
            return (
                "needs_attention",
                "publish_skipped",
                "An accepted GitHub action was skipped during publication.",
            )
    if task and task["lifecycle"] in {"failed", "canceled"}:
        return (
            "needs_attention",
            "task_failed" if task["lifecycle"] == "failed" else "task_canceled",
            "The current task did not complete successfully.",
        )
    if attempt and attempt["status"] == "stale":
        return (
            "needs_attention",
            "worker_stale",
            "The current worker attempt stopped updating its heartbeat.",
        )
    if any(action["audit_status"] in {"failed", "policy_violation"} for action in actions):
        return (
            "needs_attention",
            "github_action_rejected",
            "A planned GitHub action failed policy or audit checks.",
        )
    if workstream["workstream_lifecycle"] == "failed":
        return (
            "needs_attention",
            "workstream_failed",
            "The GitHub workstream is in a failed state.",
        )
    if (
        workstream["workstream_lifecycle"] == "waiting_for_user"
        or (task and task["lifecycle"] == "waiting_for_user")
    ):
        return (
            "waiting",
            "waiting_for_user",
            "The agent is waiting for a trusted GitHub user response.",
        )
    if any(
        action["audit_status"] == "accepted" and action["publish_status"] == "not_published"
        for action in actions
    ):
        return (
            "working",
            "waiting_publish",
            "An accepted GitHub action is waiting for publication.",
        )
    if results:
        metadata = _decode_json(results[0].get("metadata_json"), {}) or {}
        if not isinstance(metadata, dict) or not metadata.get("audit"):
            return (
                "working",
                "result_pending_audit",
                "The worker result is waiting for the next audit cycle.",
            )
    if task and task["lifecycle"] in ACTIVE_TASK_LIFECYCLES:
        return (
            "working",
            "agent_working",
            "The agent is actively processing this GitHub work item.",
        )
    if workstream["workstream_lifecycle"] == "active":
        return (
            "working",
            "active_workstream",
            "The GitHub workstream remains active.",
        )
    return (
        "history",
        "inactive",
        "The GitHub workstream no longer requires active attention.",
    )


def _source_payload(workstream):
    return {
        "source_id": workstream.get("primary_source_id"),
        "repo_id": workstream.get("repo_id"),
        "repo_full_name": workstream.get("repo_full_name"),
        "type": workstream.get("source_type"),
        "number": workstream.get("source_number"),
        "title": workstream.get("source_title") or "Untitled GitHub work item",
        "url": workstream.get("github_url"),
        "state": workstream.get("source_state"),
        "author_login": workstream.get("author_login"),
        "updated_at": workstream.get("source_updated_at"),
        "source_key": workstream.get("source_key"),
    }


def _phase_for_attempt(state, attempt):
    if not attempt:
        return None
    phases = state["phases"].get(attempt["attempt_id"], [])
    return phases[-1]["phase"] if phases else None


def _item_from_workstream(state, workstream):
    tasks = state["tasks"].get(workstream["workstream_id"], [])
    task = _current_task(workstream, tasks)
    task_id = task["task_id"] if task else None
    task_attempts = state["attempts"].get(task_id, []) if task_id else []
    attempt = task_attempts[0] if task_attempts else None
    results = state["results"].get(task_id, []) if task_id else []
    actions = state["actions"].get(task_id, []) if task_id else []
    notifications = state["notifications"].get(task_id, []) if task_id else []
    bucket, reason_code, reason_summary = _derive_operator_state(
        workstream,
        task,
        attempt,
        results,
        actions,
    )
    failed_publish_actions = sum(
        1
        for action in actions
        if action["publish_status"] != "published" and _publish_error(action)
    )
    item = {
        "id": workstream["workstream_id"],
        "bucket": bucket,
        "reason_code": reason_code,
        "reason_summary": reason_summary,
        "priority": task["priority"] if task else "P2",
        "updated_at": (task or workstream).get("updated_at"),
        "source": _source_payload(workstream),
        "agent": {
            "task_id": task_id,
            "route_id": task.get("route_id") if task else None,
            "attempt_id": attempt.get("attempt_id") if attempt else None,
            "attempt_no": attempt.get("attempt_no") if attempt else None,
            "attempt_status": attempt.get("status") if attempt else None,
            "phase": _phase_for_attempt(state, attempt),
            "heartbeat_at": attempt.get("heartbeat_at") if attempt else None,
        },
        "signals": {
            "result_pending_audit": reason_code == "result_pending_audit",
            "pending_publish_actions": sum(
                1
                for action in actions
                if action["audit_status"] == "accepted"
                and action["publish_status"] == "not_published"
            ),
            "failed_publish_actions": failed_publish_actions,
            "worker_resume_count": sum(
                1
                for notification in notifications
                if notification["notification_type"] == "worker_resume_prepared"
            ),
        },
        "task_ids": [candidate["task_id"] for candidate in tasks],
    }
    return item


def _parse_query(query):
    qualifiers = defaultdict(list)
    terms = []
    try:
        tokens = shlex.split(query or "")
    except ValueError as exc:
        raise WorkItemQueryError("invalid query") from exc
    for token in tokens:
        if ":" in token:
            name, value = token.split(":", 1)
            if name in {"repo", "is", "author", "task"} and value:
                qualifiers[name].append(value.lower())
                continue
        terms.append(token.lower())
    return qualifiers, terms


def _text_contains_all(values, terms):
    text = " ".join(str(value or "") for value in values).lower()
    return all(term in text for term in terms)


def _matches_query(item, qualifiers, terms):
    source = item["source"]
    if qualifiers["repo"] and not any(
        value in {
            str(source.get("repo_id") or "").lower(),
            str(source.get("repo_full_name") or "").lower(),
        }
        for value in qualifiers["repo"]
    ):
        return False
    if qualifiers["author"] and str(source.get("author_login") or "").lower() not in qualifiers["author"]:
        return False
    if qualifiers["task"] and not all(
        any(value in task_id.lower() for task_id in item["task_ids"])
        for value in qualifiers["task"]
    ):
        return False
    for value in qualifiers["is"]:
        if value == "active" and item["bucket"] == "history":
            return False
        if value in {"attention", "needs_attention"} and item["bucket"] != "needs_attention":
            return False
        if value in {"working", "waiting", "history"} and item["bucket"] != value:
            return False
        if value in {"open", "closed"} and str(source.get("state") or "").lower() != value:
            return False
    return _text_contains_all(
        [
            source.get("title"),
            source.get("number"),
            source.get("source_key"),
            *item["task_ids"],
        ],
        terms,
    )


def _sort_items(items, sort):
    if sort == "updated-asc":
        return sorted(items, key=lambda item: (item["updated_at"] or "", item["id"]))
    if sort == "updated-desc":
        return sorted(
            items,
            key=lambda item: (item["updated_at"] or "", item["id"]),
            reverse=True,
        )
    newest_first = sorted(
        items,
        key=lambda item: (item["updated_at"] or "", item["id"]),
        reverse=True,
    )
    return sorted(
        newest_first,
        key=lambda item: (
            BUCKET_ORDER[item["bucket"]],
            PRIORITY_ORDER.get(item["priority"], 99),
        ),
    )


def list_work_items(
    db_path,
    *,
    bucket=None,
    repo=None,
    actor=None,
    query="",
    sort="priority",
    limit=30,
    cursor=None,
):
    """Return a filtered and paginated PR/Issue-first work list."""

    if bucket and bucket not in BUCKETS:
        raise WorkItemQueryError("invalid bucket")
    if sort not in SORTS:
        raise WorkItemQueryError("invalid sort")
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise WorkItemQueryError("limit must be between 1 and 100")
    qualifiers, terms = _parse_query(query)
    with closing(_connect(db_path)) as conn:
        state = _load_state(conn)
    items = [
        _item_from_workstream(state, workstream)
        for workstream in state["workstreams"]
    ]
    repositories = sorted(
        {
            (item["source"]["repo_id"], item["source"]["repo_full_name"])
            for item in items
            if item["source"]["repo_id"] and item["source"]["repo_full_name"]
        },
        key=lambda value: (value[1].lower(), value[0]),
    )
    actors = sorted(
        {
            item["source"]["author_login"]
            for item in items
            if item["source"]["author_login"]
        },
        key=str.lower,
    )
    items = [
        item
        for item in items
        if (not repo or repo in {item["source"]["repo_id"], item["source"]["repo_full_name"]})
        and (not actor or actor == item["source"]["author_login"])
        and _matches_query(item, qualifiers, terms)
    ]
    counts = Counter(item["bucket"] for item in items)
    if bucket:
        items = [item for item in items if item["bucket"] == bucket]
    elif not qualifiers["is"]:
        items = [item for item in items if item["bucket"] != "history"]
    items = _sort_items(items, sort)
    if cursor:
        cursor_pair = _decode_cursor(cursor)
        try:
            cursor_index = next(
                index
                for index, item in enumerate(items)
                if (item["updated_at"] or "", item["id"]) == cursor_pair
            )
        except StopIteration as exc:
            raise WorkItemQueryError("invalid cursor") from exc
        items = items[cursor_index + 1 :]
    page = items[:limit]
    next_cursor = None
    if len(items) > limit and page:
        next_cursor = _encode_cursor(page[-1]["updated_at"] or "", page[-1]["id"])
    for item in page:
        item.pop("task_ids", None)
    return {
        "items": page,
        "counts": {name: counts.get(name, 0) for name in BUCKET_ORDER},
        "repositories": [
            {"repo_id": repo_id, "full_name": full_name}
            for repo_id, full_name in repositories
        ],
        "actors": actors,
        "next_cursor": next_cursor,
    }


def _safe_failure(value):
    failure = _decode_json(value, {}) or {}
    if not isinstance(failure, dict):
        return None
    return failure.get("safe_error") or failure.get("message") or failure.get("reason")


def _safe_result(row):
    metadata = _decode_json(row.get("metadata_json"), {}) or {}
    audit = metadata.get("audit") if isinstance(metadata, dict) else None
    usage = metadata.get("usage") if isinstance(metadata, dict) else None
    payload = {
        "result_id": row["result_id"],
        "task_id": row["task_id"],
        "attempt_id": row["attempt_id"],
        "output_type": row["output_type"],
        "verification": _decode_json(row.get("verification_json"), []) or [],
        "handoff": row.get("handoff") or "",
        "created_at": row["created_at"],
        "audit_status": audit.get("status") if isinstance(audit, dict) else None,
    }
    if isinstance(usage, dict):
        payload["usage"] = {
            key: usage.get(key)
            for key in ("usage_available", "source", "usage", "total_cost_usd")
            if key in usage
        }
    return payload


def _safe_action(row):
    metadata = _decode_json(row.get("metadata_json"), {}) or {}
    publish = metadata.get("publish") if isinstance(metadata, dict) else None
    return {
        "action_id": row["action_id"],
        "result_id": row["result_id"],
        "task_id": row["task_id"],
        "action_type": row["action_type"],
        "target_url": row["target_url"],
        "external_id": row["external_id"],
        "audit_status": row["audit_status"],
        "publish_status": row["publish_status"],
        "safe_error": publish.get("safe_error") if isinstance(publish, dict) else None,
        "created_at": row["created_at"],
    }


def get_work_item(db_path, workstream_id):
    """Return the structured detail for one workstream."""

    with closing(_connect(db_path)) as conn:
        state = _load_state(conn)
        workstream = next(
            (
                candidate
                for candidate in state["workstreams"]
                if candidate["workstream_id"] == workstream_id
            ),
            None,
        )
        if not workstream:
            return None
        item = _item_from_workstream(state, workstream)
        related_sources = [
            {
                "relationship": row["relationship"],
                "source_id": row["source_id"],
                "source_key": row["source_key"],
                "type": row["source_type"],
                "number": row["number"],
                "title": row["title"],
                "url": row["html_url"],
                "state": row["state"],
            }
            for row in conn.execute(
                """
                SELECT
                  ws.relationship, gs.source_id, gs.source_key, gs.source_type,
                  gs.number, gs.title, gs.html_url, gs.state
                FROM workstream_sources ws
                JOIN github_sources gs ON gs.source_id = ws.source_id
                WHERE ws.workstream_id = ?
                ORDER BY ws.created_at, gs.source_id
                """,
                (workstream_id,),
            )
        ]
    tasks = state["tasks"].get(workstream_id, [])
    task_ids = {task["task_id"] for task in tasks}
    attempts = [row for task_id in task_ids for row in state["attempts"].get(task_id, [])]
    attempt_ids = {attempt["attempt_id"] for attempt in attempts}
    phases = [row for attempt_id in attempt_ids for row in state["phases"].get(attempt_id, [])]
    results = [row for task_id in task_ids for row in state["results"].get(task_id, [])]
    actions = [row for task_id in task_ids for row in state["actions"].get(task_id, [])]
    artifacts = [row for task_id in task_ids for row in state["artifacts"].get(task_id, [])]
    return {
        "id": workstream_id,
        "source": item["source"],
        "related_sources": related_sources,
        "operator_state": {
            "bucket": item["bucket"],
            "reason_code": item["reason_code"],
            "reason_summary": item["reason_summary"],
        },
        "tasks": [
            {
                "task_id": row["task_id"],
                "parent_task_id": row["parent_task_id"],
                "lifecycle": row["lifecycle"],
                "priority": row["priority"],
                "route_id": row["route_id"],
                "expected_output": row["expected_output"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in sorted(tasks, key=lambda row: (row["created_at"], row["task_id"]))
        ],
        "attempts": [
            {
                "attempt_id": row["attempt_id"],
                "task_id": row["task_id"],
                "attempt_no": row["attempt_no"],
                "status": row["status"],
                "worktree_path": row["worktree_path"],
                "branch_name": row["branch_name"],
                "started_at": row["started_at"],
                "heartbeat_at": row["heartbeat_at"],
                "finished_at": row["finished_at"],
                "failure_summary": _safe_failure(row["failure_json"]),
            }
            for row in attempts
        ],
        "phases": phases,
        "results": [_safe_result(row) for row in results],
        "actions": [_safe_action(row) for row in actions],
        "artifacts": [
            {
                "artifact_id": row["artifact_id"],
                "task_id": row["task_id"],
                "attempt_id": row["attempt_id"],
                "artifact_type": row["artifact_type"],
                "bytes": row["bytes"],
                "created_at": row["created_at"],
                "preview_available": True,
            }
            for row in artifacts
        ],
    }
