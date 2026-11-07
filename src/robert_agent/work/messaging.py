"""Attention/outbox facts shared by operator ingress and notification delivery."""

import json

from robert_agent.work.inputs import QUESTION_EVENTS, FAILURE_EVENTS, DECISION_EVENTS

ATTENTION_EVENTS = QUESTION_EVENTS | FAILURE_EVENTS | DECISION_EVENTS


def record_attention(conn, *, event_id, work_item_id, event_type, metadata, resolves_event_id, now):
    """Participate in the caller's event transaction; never commit or send."""
    if resolves_event_id:
        conn.execute(
            "UPDATE notifications SET status='resolved' WHERE notification_id=? OR json_extract(metadata_json,'$.attention_event_id')=?",
            ("attention-" + resolves_event_id, resolves_event_id),
        )
    if event_type not in ATTENTION_EVENTS:
        return
    conn.execute(
        """INSERT OR IGNORE INTO notifications
        (notification_id,task_id,notification_type,channel,status,created_at,metadata_json)
        VALUES (?,?,?,'local','recorded',?,?)
        ON CONFLICT(notification_id) DO UPDATE SET metadata_json=json_patch(notifications.metadata_json,excluded.metadata_json)""",
        (
            metadata.get("notification_id") or "attention-" + event_id,
            metadata.get("task_id"),
            event_type,
            now,
            json.dumps({"work_item_id": work_item_id, "attention_event_id": event_id}),
        ),
    )


def notification_scope(conn, task_id, metadata):
    """Return repository identity without inferring authority from chat text."""
    item_id = metadata.get("work_item_id")
    if item_id:
        row = conn.execute("SELECT repo_id FROM work_items WHERE work_item_id=?", (item_id,)).fetchone()
    elif task_id:
        row = conn.execute(
            "SELECT w.repo_id FROM tasks t JOIN workstreams w USING(workstream_id) WHERE t.task_id=?", (task_id,)
        ).fetchone()
    else:
        repo_id = metadata.get("repo_id")
        return repo_id
    return row[0] if row else None


def attention_details(conn, metadata, *, include_closed=False):
    event_id = metadata.get("attention_event_id")
    if not event_id:
        return None
    row = conn.execute(
        """SELECT e.event_id,e.work_item_id,e.event_type,e.body,e.metadata_json,
        wi.version,wi.title,wi.completed_at,wi.canceled_at,
        EXISTS(SELECT 1 FROM work_item_events r WHERE r.resolves_event_id=e.event_id)
        FROM work_item_events e JOIN work_items wi USING(work_item_id)
        WHERE e.event_id=? AND e.work_item_id=?""",
        (event_id, metadata.get("work_item_id")),
    ).fetchone()
    if not row or (not include_closed and (row[7] or row[8] or row[9])):
        return None
    return {
        "event_id": row[0],
        "work_item_id": row[1],
        "event_type": row[2],
        "summary": row[3],
        "choices": json.loads(row[4] or "{}").get("choices", []),
        "kind": json.loads(row[4] or "{}").get("kind"),
        "version": row[5],
        "title": row[6],
    }


def actions_for(event_type, kind=None):
    if kind == "result_contract_cutover":
        return ["cancel"]
    if event_type == "operator_question":
        return ["reply", "cancel"]
    if event_type in {"completion_acceptance_requested", "operator_decision_required"}:
        return ["approve", "cancel"]
    if event_type == "publication_failed":
        return ["retry_publish", "cancel"]
    if event_type in FAILURE_EVENTS:
        return ["retry", "cancel"]
    return ["ack"]
