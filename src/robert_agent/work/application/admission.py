"""Reauthorize preserved requests and append input under execution ownership."""

import json
from robert_agent.work import source_policy
from robert_agent.execution import ownership as execution_ownership
from robert_agent.work import queries as work_queries


def append_event_to_active_task(conn, event, relationship, run_now):
    task_id = work_queries.active_task_id(conn, event["workstream_id"])
    if not task_id:
        return False
    if not execution_ownership.claim_task(conn, task_id, run_now):
        return False
    conn.execute(
        """
        INSERT OR IGNORE INTO task_events(task_id, event_id, relationship, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (task_id, f"event:{event['event_fingerprint']}", relationship, run_now),
    )
    conn.execute(
        """
        UPDATE workstreams
        SET updated_at = ?
        WHERE workstream_id = ?
        """,
        (run_now, event["workstream_id"]),
    )
    return True


def _json_object(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def ordinary_dispatch_allowed(conn, task_id, repo):
    row = conn.execute(
        "SELECT task_kind,metadata_json FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if not row or row[0] != "human_request":
        return True  # System remediation is governed by its Episode policy.
    metadata = _json_object(row[1])
    item = conn.execute(
        "SELECT wi.origin_type FROM work_items wi JOIN tasks t ON t.workstream_id=wi.workstream_id WHERE t.task_id=?",
        (task_id,),
    ).fetchone()
    if (item and item[0] == "web") or metadata.get("trigger_work_item_event_id"):
        return True
    events = work_queries.task_related_events(conn, task_id, include_consumed=True)
    if not events:
        return False
    return any(
        source_policy.reauthorize_saved_request(
            event,
            repo,
            known_workstreams=[]
            if event.get("event_type") in {"assigned", "review_request", "notification"}
            else [event["workstream_id"]],
        ).get("drives_execution")
        for event in events
    )
