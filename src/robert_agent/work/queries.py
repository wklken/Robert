"""Read task and source facts without importing the runtime driver."""

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_sources as db_read_github_sources
from robert_agent.persistence.queries import task_events as db_read_task_events
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import workstreams as db_read_workstreams
import json


def load_active_workstreams(conn, repo_id):
    return {row[0] for row in db_read_workstreams.load_active_workstreams(conn, repo_id)}


def load_known_workstreams(conn, repo_id):
    return {row[0] for row in db_read_workstreams.load_known_workstreams(conn, repo_id)}


def task_related_events(conn, task_id, include_consumed=False):
    relationships = ["trigger", "context", "pending"]
    if include_consumed:
        relationships.append("consumed")
    rows = db_read_task_events.task_related_events_filtered(conn, relationships, task_id)
    events = []
    consumed_events = []
    for relationship, event_fingerprint, payload in rows:
        event = json.loads(payload)
        event.setdefault("event_fingerprint", event_fingerprint)
        if relationship == "consumed":
            consumed_events.append(event)
        else:
            events.append(event)
    # Enrich recovery content while retaining the original trigger authority.
    metadata = db_records.first(db_read_tasks.find_metadata_json_by_task_id(conn, task_id))
    refreshed = json.loads(metadata[0] or "{}").get("refreshed_context", []) if metadata else []
    by_fingerprint = {item["event_fingerprint"]: item for item in refreshed}
    for event in events + consumed_events:
        snapshot = by_fingerprint.get(event.get("event_fingerprint"), {})
        event.update(
            {
                key: snapshot[key]
                for key in (
                    "body",
                    "title",
                    "source_updated_at",
                    "body_observed",
                    "body_updated_at",
                    "source_body",
                    "operator_responses",
                )
                if key in snapshot
            }
        )
    return events + consumed_events


def failed_parent_context_events(conn, parent_task_id):
    if not parent_task_id:
        return []
    row = db_records.first(db_read_tasks.find_lifecycle_by_task_id(conn, parent_task_id))
    if not row or row[0] != "failed":
        return []
    rows = db_read_task_events.find_for_failed_parent_context_events(conn, parent_task_id)
    events = []
    for relationship, event_fingerprint, payload in rows:
        event = json.loads(payload)
        event.setdefault("event_fingerprint", event_fingerprint)
        if relationship == "trigger" or event.get("mentions_dd"):
            events.append(event)
    return events


def latest_task_id(conn, workstream_id):
    row = db_records.first(db_read_tasks.find_task_id_by_workstream_id_2(conn, workstream_id))
    return row[0] if row else None


def active_task_id(conn, workstream_id):
    row = db_records.first(db_read_workstreams.find_active_task_id_by_workstream_id(conn, workstream_id))
    if row and row[0]:
        return row[0]
    row = db_records.first(db_read_tasks.find_for_active_task_id(conn, workstream_id))
    return row[0] if row else None


def remote_source_rows(conn, repo_id):
    return db_read_workstreams.find_for_remote_source_rows(conn, repo_id)


def sources_for_workstream(conn, workstream_id):
    return [
        {
            "source_type": source_type,
            "number": number,
        }
        for source_type, number in db_read_github_sources.find_for_sources_for_workstream(
            conn, workstream_id, workstream_id
        )
    ]
