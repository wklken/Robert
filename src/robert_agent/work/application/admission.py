"""Reauthorize preserved requests and append input under execution ownership."""

from robert_agent.foundation.contracts.work import TaskKind

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import work_items as db_read_work_items
from robert_agent.persistence.stores import task_events as db_write_task_events
from robert_agent.persistence.stores import workstreams as db_write_workstreams
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
    db_write_task_events.insert_for_append_event_to_active_task(
        conn, task_id, f"event:{event['event_fingerprint']}", relationship, run_now
    )
    db_write_workstreams.set_updated_at_by_workstream_id(conn, run_now, event["workstream_id"])
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
    row = db_records.first(db_read_tasks.find_task_kind_and_metadata_json_by_task_id(conn, task_id))
    if not row or row[0] != TaskKind.HUMAN_REQUEST.value:
        return True  # System remediation is governed by its Episode policy.
    metadata = _json_object(row[1])
    item = db_records.first(db_read_work_items.find_for_ordinary_dispatch_allowed(conn, task_id))
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
