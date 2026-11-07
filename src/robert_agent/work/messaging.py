"""Attention/outbox facts shared by operator ingress and notification delivery."""

import json

from robert_agent.persistence import records
from robert_agent.persistence.queries import notifications, work_item_events
from robert_agent.persistence.stores import notifications as notification_store
from robert_agent.work.inputs import QUESTION_EVENTS, FAILURE_EVENTS, DECISION_EVENTS

ATTENTION_EVENTS = QUESTION_EVENTS | FAILURE_EVENTS | DECISION_EVENTS


def record_attention(conn, *, event_id, work_item_id, event_type, metadata, resolves_event_id, now):
    """Participate in the caller's event transaction; never commit or send."""
    if resolves_event_id:
        notification_store.resolve_attention(conn, resolves_event_id)
    if event_type not in ATTENTION_EVENTS:
        return
    notification_store.record_attention(
        conn,
        notification_id=metadata.get("notification_id") or "attention-" + event_id,
        task_id=metadata.get("task_id"),
        event_type=event_type,
        now=now,
        metadata_json=json.dumps({"work_item_id": work_item_id, "attention_event_id": event_id}),
    )


def notification_scope(conn, task_id, metadata):
    """Return repository identity without inferring authority from chat text."""
    item_id = metadata.get("work_item_id")
    if item_id or task_id:
        row = records.first(notifications.find_scope(conn, work_item_id=item_id, task_id=task_id))
        return (row[0] if row else None), True
    repo_id = metadata.get("repo_id")
    if repo_id is not None or metadata.get("repository_scoped"):
        return repo_id, True
    if metadata.get("incident_id"):
        row = records.first(notifications.find_incident_scope(conn, metadata["incident_id"]))
        if row and row[0] == "controller":
            return None, False
        return (row[1] if row and row[0] in {"github_event", "work_item_event"} else None), True
    return None, False


def attention_details(conn, metadata, *, include_closed=False):
    event_id = metadata.get("attention_event_id")
    if not event_id:
        return None
    row = records.first(work_item_events.find_attention(conn, event_id, metadata.get("work_item_id")))
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
