"""Deterministic read models for the DD repository task board."""

import base64
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit


COLUMNS = (
    ("backlog", "Backlog"),
    ("todo", "TODO"),
    ("doing", "Doing"),
    ("waiting", "Waiting for You"),
    ("review", "Review"),
    ("done", "Done"),
)
ATTENTION_EVENTS = frozenset(
    {
        "operator_question",
        "completion_acceptance_requested",
        "operator_decision_required",
        "execution_failed",
        "publication_failed",
        "manual_worker_unavailable",
        "review_attention",
        "unmerged_pr_closed",
    }
)
SAFE_EVENT_METADATA_KEYS = frozenset(
    {
        "kind",
        "task_id",
        "result_id",
        "source_id",
        "source_key",
        "workstream_id",
        "pr_number",
        "url",
        "route_confidence",
        "requested_worker",
    }
)


class BoardQueryError(ValueError):
    pass


def _connect(db_path):
    conn = sqlite3.connect(Path(db_path).expanduser())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _decode_json(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _cursor_encode(values):
    raw = json.dumps(values, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _cursor_decode(value):
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BoardQueryError("invalid cursor") from exc
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or not all(isinstance(part, str) and part for part in decoded)
    ):
        raise BoardQueryError("invalid cursor")
    return tuple(decoded)


def _safe_event_metadata(value):
    metadata = _decode_json(value, {})
    if not isinstance(metadata, dict):
        return {}
    return {
        key: metadata[key]
        for key in SAFE_EVENT_METADATA_KEYS
        if key in metadata and isinstance(metadata[key], (str, int, float, bool, type(None)))
    }


def _safe_http_url(value):
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _unresolved_attention(conn, work_item_id):
    placeholders = ",".join("?" for _event_type in ATTENTION_EVENTS)
    rows = conn.execute(
        f"""
        SELECT e.event_id, e.event_type, e.body, e.created_at, e.metadata_json
        FROM work_item_events e
        WHERE e.work_item_id = ?
          AND e.event_type IN ({placeholders})
          AND NOT EXISTS (
            SELECT 1 FROM work_item_events response
            WHERE response.resolves_event_id = e.event_id
          )
        ORDER BY e.created_at DESC, e.event_id DESC
        """,
        [work_item_id] + sorted(ATTENTION_EVENTS),
    ).fetchall()
    return [
        {
            "event_id": row["event_id"],
            "type": row["event_type"],
            "summary": row["body"][:500],
            "created_at": row["created_at"],
            "metadata": _safe_event_metadata(row["metadata_json"]),
        }
        for row in rows
    ]


def _latest_task(conn, workstream_id):
    return conn.execute(
        """
        SELECT task_id, lifecycle, priority, route_id, expected_output,
               routing_mode, requested_worker, created_at, updated_at
        FROM tasks
        WHERE workstream_id = ?
        ORDER BY created_at DESC, task_id DESC
        LIMIT 1
        """,
        (workstream_id,),
    ).fetchone()


def _latest_attempt(conn, workstream_id):
    return conn.execute(
        """
        SELECT a.attempt_id, a.status, a.branch_name, a.worktree_path,
               a.started_at, a.finished_at
        FROM attempts a
        JOIN tasks t ON t.task_id = a.task_id
        WHERE t.workstream_id = ?
        ORDER BY a.started_at DESC, a.attempt_id DESC
        LIMIT 1
        """,
        (workstream_id,),
    ).fetchone()


def _derived_pr(conn, root_workstream_id):
    return conn.execute(
        """
        SELECT gs.number, gs.html_url, gs.state, w.workstream_id
        FROM workstreams w
        JOIN github_sources gs ON gs.source_id = w.primary_source_id
        WHERE w.origin_workstream_id = ?
          AND gs.source_type = 'pull_request'
        ORDER BY w.updated_at DESC, gs.number DESC
        LIMIT 1
        """,
        (root_workstream_id,),
    ).fetchone()


def _valid_commands(item, column, attention, *, running=False):
    if column == "backlog":
        return ["edit", "start", "cancel"]
    if column == "waiting":
        if attention and attention[0]["type"] == "operator_decision_required":
            return ["approve"] if running else ["approve", "cancel"]
        commands = ["approve", "request_changes", "cancel"]
        if any(signal["type"] in {"operator_question", "completion_acceptance_requested"} for signal in attention):
            commands.insert(1, "reply")
        if any(signal["type"] in ATTENTION_EVENTS - {"operator_question", "completion_acceptance_requested", "operator_decision_required"} for signal in attention):
            commands.insert(-1, "retry")
        return commands
    if column == "review":
        return ["request_changes", "cancel"] if attention else ["request_changes"]
    if column == "done":
        return ["reopen"]
    if column == "todo":
        return ["cancel"]
    return []


def _project_item(conn, row):
    work_item_id = row["work_item_id"]
    workstream_id = row["workstream_id"]
    attention = _unresolved_attention(conn, work_item_id)
    task = _latest_task(conn, workstream_id) if workstream_id else None
    attempt = _latest_attempt(conn, workstream_id) if workstream_id else None
    pr = _derived_pr(conn, workstream_id) if workstream_id else None
    running = bool(
        workstream_id
        and conn.execute(
            """
            SELECT 1 FROM attempts a
            JOIN tasks t ON t.task_id = a.task_id
            WHERE t.workstream_id = ? AND a.status = 'running'
            LIMIT 1
            """,
            (workstream_id,),
        ).fetchone()
    )
    publication_in_progress = bool(
        workstream_id
        and conn.execute(
            """
            SELECT 1 FROM github_actions ga
            JOIN tasks t ON t.task_id = ga.task_id
            WHERE t.workstream_id = ?
              AND (ga.audit_status = 'pending'
                   OR (ga.audit_status = 'accepted' AND ga.publish_status = 'not_published'))
            LIMIT 1
            """,
            (workstream_id,),
        ).fetchone()
    )

    if row["canceled_at"]:
        column, reason_code, reason_summary = "history", "canceled", "Canceled by the operator"
    elif row["completed_at"]:
        column, reason_code, reason_summary = "done", "completed", "Completion is satisfied"
    elif attention and not (pr and attention[0]["type"] in {"publication_failed", "unmerged_pr_closed", "review_attention"}):
        column, reason_code, reason_summary = "waiting", attention[0]["type"], attention[0]["summary"] or "Operator input is required"
    elif running or publication_in_progress:
        column, reason_code, reason_summary = "doing", "running", "Agent execution or publication is in progress"
    elif pr and attention and attention[0]["type"] in {"publication_failed", "unmerged_pr_closed", "review_attention"}:
        column, reason_code, reason_summary = "review", attention[0]["type"], attention[0]["summary"] or "Pull request review needs attention"
    elif task and task["lifecycle"] in {"detected", "authorized", "classified", "queued", "running"}:
        column, reason_code, reason_summary = "todo", task["lifecycle"], "Runnable work is queued"
    elif pr and (pr["state"] or "open").lower() == "open":
        column, reason_code, reason_summary = "review", "open_pr", f"PR #{pr['number']} is open"
    elif not row["activated_at"]:
        column, reason_code, reason_summary = "backlog", "draft", "Requirement is editable"
    else:
        column, reason_code, reason_summary = "todo", "activated", "Activated work is pending"

    routing_mode = task["routing_mode"] if task else row["routing_mode"]
    requested_worker = task["requested_worker"] if task else row["requested_worker"]
    card = {
        "work_item_id": work_item_id,
        "repo_id": row["repo_id"],
        "repo_full_name": row["repo_full_name"],
        "title": row["title"],
        "priority": row["priority"],
        "origin_type": row["origin_type"],
        "column": column,
        "reason_code": reason_code,
        "reason_summary": reason_summary,
        "routing_mode": routing_mode,
        "agent": requested_worker if routing_mode == "manual" else "Auto",
        "task_id": task["task_id"] if task else None,
        "task_lifecycle": task["lifecycle"] if task else None,
        "branch": attempt["branch_name"] if attempt else None,
        "worktree_label": Path(attempt["worktree_path"]).name if attempt and attempt["worktree_path"] else None,
        "pr": (
            {
                "number": pr["number"],
                "url": _safe_http_url(pr["html_url"]),
                "state": pr["state"],
            }
            if pr
            else None
        ),
        "attention_signals": attention,
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    card["valid_commands"] = _valid_commands(row, column, attention, running=running)
    return card


def _all_projections(conn):
    rows = conn.execute(
        """
        SELECT wi.*, r.full_name AS repo_full_name
        FROM work_items wi
        JOIN repos r ON r.repo_id = wi.repo_id
        ORDER BY wi.updated_at DESC, wi.work_item_id DESC
        """
    ).fetchall()
    return [_project_item(conn, row) for row in rows]


def _matches(item, *, repo, agent, priority, attention, query):
    if repo and repo not in {item["repo_id"], item["repo_full_name"]}:
        return False
    if agent and item["agent"] != agent:
        return False
    if priority and item["priority"] != priority:
        return False
    if attention is not None:
        wanted = attention in {True, "true", "1", "yes"}
        if bool(item["attention_signals"]) != wanted:
            return False
    if query:
        haystack = " ".join(
            str(value or "")
            for value in (
                item["title"], item["work_item_id"], item["task_id"], item["branch"],
                (item["pr"] or {}).get("number"),
            )
        ).lower()
        if query.lower() not in haystack:
            return False
    return True


def list_board(
    db_path,
    *,
    repo=None,
    column=None,
    agent=None,
    priority=None,
    attention=None,
    query="",
    limit=50,
    cursor=None,
    include_history=False,
):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise BoardQueryError("limit must be between 1 and 200")
    valid_columns = {column_id for column_id, _label in COLUMNS} | {"history"}
    if column and column not in valid_columns:
        raise BoardQueryError("invalid column")
    if priority and priority not in {"P0", "P1", "P2", "P3"}:
        raise BoardQueryError("invalid priority")
    cursor_value = _cursor_decode(cursor)
    with closing(_connect(db_path)) as conn:
        projections = [
            item
            for item in _all_projections(conn)
            if _matches(
                item,
                repo=repo,
                agent=agent,
                priority=priority,
                attention=attention,
                query=query,
            )
            and (include_history or item["column"] != "history")
        ]
        counts = {
            column_id: sum(1 for item in projections if item["column"] == column_id)
            for column_id, _label in COLUMNS
        }
        if cursor_value:
            projections = [
                item
                for item in projections
                if (item["updated_at"], item["work_item_id"]) < cursor_value
            ]
        if column:
            projections = [item for item in projections if item["column"] == column]
        page = projections[: limit + 1]
        has_more = len(page) > limit
        items = page[:limit]
        next_cursor = (
            _cursor_encode([items[-1]["updated_at"], items[-1]["work_item_id"]])
            if has_more and items
            else None
        )
        running_rows = conn.execute(
            """
            SELECT w.repo_id, COUNT(*)
            FROM attempts a
            JOIN tasks t ON t.task_id = a.task_id
            JOIN workstreams w ON w.workstream_id = t.workstream_id
            WHERE a.status = 'running'
            GROUP BY w.repo_id
            """
        ).fetchall()
        per_repo_running = {row[0]: row[1] for row in running_rows}
        repos = [
            {"repo_id": row[0], "full_name": row[1], "running": per_repo_running.get(row[0], 0)}
            for row in conn.execute("SELECT repo_id, full_name FROM repos ORDER BY full_name")
        ]
        agents = sorted({item["agent"] for item in projections} | {"Auto"})
    return {
        "columns": [
            {"id": column_id, "label": label, "count": counts[column_id]}
            for column_id, label in COLUMNS
        ],
        "counts": counts,
        "capacity": {
            "running": sum(per_repo_running.values()),
            "by_repo": per_repo_running,
        },
        "filters": {
            "repos": repos,
            "agents": agents,
            "priorities": ["P0", "P1", "P2", "P3"],
        },
        "items": items,
        "next_cursor": next_cursor,
    }


def get_work_item_detail(db_path, work_item_id):
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            """
            SELECT wi.*, r.full_name AS repo_full_name
            FROM work_items wi JOIN repos r ON r.repo_id = wi.repo_id
            WHERE wi.work_item_id = ?
            """,
            (work_item_id,),
        ).fetchone()
        if not row:
            return None
        card = _project_item(conn, row)
        tasks = []
        if row["workstream_id"]:
            task_rows = conn.execute(
                """
                SELECT task_id, parent_task_id, lifecycle, priority, route_id,
                       expected_output, routing_mode, requested_worker, created_at, updated_at
                FROM tasks WHERE workstream_id = ?
                ORDER BY created_at, task_id
                """,
                (row["workstream_id"],),
            ).fetchall()
            for task in task_rows:
                attempts = [
                    {
                        "attempt_id": attempt["attempt_id"],
                        "status": attempt["status"],
                        "branch": attempt["branch_name"],
                        "worktree_label": Path(attempt["worktree_path"]).name if attempt["worktree_path"] else None,
                        "started_at": attempt["started_at"],
                        "finished_at": attempt["finished_at"],
                    }
                    for attempt in conn.execute(
                        """
                        SELECT attempt_id, status, branch_name, worktree_path,
                               started_at, finished_at
                        FROM attempts WHERE task_id = ? ORDER BY attempt_no
                        """,
                        (task["task_id"],),
                    )
                ]
                tasks.append(
                    {
                        "task_id": task["task_id"],
                        "parent_task_id": task["parent_task_id"],
                        "lifecycle": task["lifecycle"],
                        "priority": task["priority"],
                        "route_id": task["route_id"],
                        "expected_output": task["expected_output"],
                        "routing_mode": task["routing_mode"],
                        "requested_worker": task["requested_worker"],
                        "created_at": task["created_at"],
                        "updated_at": task["updated_at"],
                        "attempts": attempts,
                    }
                )
        timeline = list_work_item_events(db_path, work_item_id, limit=20)
        return {
            **card,
            "description": row["description"],
            "created_by": row["created_by"],
            "activated_at": row["activated_at"],
            "completed_at": row["completed_at"],
            "canceled_at": row["canceled_at"],
            "tasks": tasks,
            "events": timeline["events"],
            "events_next_cursor": timeline["next_cursor"],
        }


def list_work_item_events(db_path, work_item_id, *, limit=50, cursor=None):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise BoardQueryError("limit must be between 1 and 200")
    cursor_value = _cursor_decode(cursor)
    with closing(_connect(db_path)) as conn:
        exists = conn.execute(
            "SELECT 1 FROM work_items WHERE work_item_id = ?",
            (work_item_id,),
        ).fetchone()
        if not exists:
            return {"events": [], "next_cursor": None}
        params = [work_item_id]
        cursor_clause = ""
        if cursor_value:
            cursor_clause = "AND (created_at, event_id) < (?, ?)"
            params.extend(cursor_value)
        rows = conn.execute(
            f"""
            SELECT event_id, event_type, actor_kind, actor_identity, body,
                   resolves_event_id, idempotency_key, created_at, metadata_json
            FROM work_item_events
            WHERE work_item_id = ? {cursor_clause}
            ORDER BY created_at DESC, event_id DESC
            LIMIT ?
            """,
            params + [limit + 1],
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        events = [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "actor_kind": row["actor_kind"],
                "actor_identity": row["actor_identity"],
                "body": row["body"],
                "resolves_event_id": row["resolves_event_id"],
                "created_at": row["created_at"],
                "metadata": _safe_event_metadata(row["metadata_json"]),
            }
            for row in rows
        ]
        next_cursor = (
            _cursor_encode([rows[-1]["created_at"], rows[-1]["event_id"]])
            if has_more and rows
            else None
        )
    return {"events": events, "next_cursor": next_cursor}
