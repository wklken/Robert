"""Persist system questions without inventing an execution or user authorization."""

import json

from robert_agent.persistence.stores import work_item_events
from robert_agent.work import messaging


def record_supervisor_question(conn, *, work_item_id, root_id, decision_id, body, now):
    event_id = decision_id + "-question"
    work_item_events.insert_event(
        conn,
        event_id,
        work_item_id,
        "operator_question",
        "system",
        "supervisor",
        body,
        None,
        event_id,
        now,
        json.dumps({"kind": "supervisor_question", "supervision_decision_id": decision_id, "workstream_id": root_id}),
    )
    messaging.record_attention(
        conn,
        event_id=event_id,
        work_item_id=work_item_id,
        event_type="operator_question",
        metadata={"notification_id": decision_id},
        resolves_event_id=None,
        now=now,
    )
    return event_id
