"""Persist source events and read their associated task state."""

from robert_agent.foundation.contracts.work import TaskLifecycle, WorkstreamLifecycle

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_events as db_read_github_events
from robert_agent.persistence.queries import task_events as db_read_task_events
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import work_item_events as db_read_work_item_events
from robert_agent.persistence.queries import workstreams as db_read_workstreams
from robert_agent.persistence.stores import github_events as db_write_github_events
from robert_agent.persistence.stores import github_sources as db_write_github_sources
from robert_agent.adapters.github import discovery as discover
from robert_agent.work.application import commands as work_items
from robert_agent.work import control_queries
from robert_agent.foundation import values
from robert_agent.work import policies as work_policies
from robert_agent.work import queries as work_queries
import json


def event_has_terminal_trigger(conn, event_fingerprint):
    row = db_records.first(db_read_task_events.find_for_event_has_terminal_trigger(conn, event_fingerprint))
    return bool(row)


def should_skip_terminal_trigger(conn, event):
    if not event_has_terminal_trigger(
        conn,
        event["event_fingerprint"],
    ):
        return False
    return event.get("actor_kind") != "github_system"


def active_task_state(conn, workstream_id):
    row = db_records.first(db_read_tasks.find_for_active_task_state(conn, workstream_id))
    if not row:
        return None
    return {
        "task_id": row[0],
        "task_lifecycle": row[1],
        "route_id": row[2],
        "expected_output": row[3],
        "attempt_id": row[4],
        "attempt_status": row[5],
    }


def waiting_task_state(conn, workstream_id):
    row = db_records.first(db_read_workstreams.find_for_waiting_task_state(conn, workstream_id))
    if not row:
        return None
    return {"task_id": row[0]}


def task_review_input_policy(conn, task_id):
    row = db_records.first(db_read_tasks.find_metadata_json_by_task_id(conn, task_id))
    return values.json_object(row[0] if row else None).get("review_input_policy", "required")


def insert_source_and_event(conn, repo_id, event, run_now):
    source_id = values.github_source_id(event["source_key"])
    db_write_github_sources.insert_source_and_event(
        conn,
        source_id,
        repo_id,
        event["source_key"],
        event["source_type"],
        event["number"],
        event.get("url"),
        event.get("title", ""),
        event.get("state") or "open",
        event.get("source_author_login", event.get("actor_login")),
        event.get("source_updated_at"),
        event.get("state"),
    )
    event_id = values.github_event_id(event["event_fingerprint"])
    previous = db_records.first(
        db_read_github_events.find_payload_json_by_event_fingerprint(conn, event["event_fingerprint"])
    )
    if previous:
        # Routing and prompt creation must use the same body as durable context.
        event.update(discover.merge_event_body(event, json.loads(previous[0])))
    db_write_github_events.insert_source_and_event(
        conn,
        event_id,
        repo_id,
        source_id,
        event["event_fingerprint"],
        event["event_type"],
        event.get("actor_kind", "github_user"),
        event.get("actor_login"),
        event.get("author_association"),
        event.get("authorization_status", "pending"),
        event.get("event_at", run_now),
        json.dumps(event, ensure_ascii=False, sort_keys=True),
    )
    return source_id, event_id


def event_existing_pr_head_sha(event):
    head_sha = event.get("existing_pr_head_sha") or event.get("head_sha")
    return head_sha if isinstance(head_sha, str) else ""


def operator_canceled_item(conn, item):
    if not item or not item["canceled_at"]:
        return False
    return bool(db_records.first(db_read_work_item_events.find_for_operator_canceled_item(conn, item["work_item_id"])))


def github_followup_allows_new_subject(conn, repo, event, parent_task_id):
    """Admit a fresh trusted request, preserving the prior execution's state."""
    if (
        not parent_task_id
        or not work_policies.is_driving_context_event(event)
        or not work_policies.is_trusted_actor(event, repo)
        or event.get("actor_kind") == "github_system"
        or event.get("actor_login") == repo["github_account"]
    ):
        return False
    if db_records.first(
        db_read_task_events.find_for_prepare_queued_reviews(conn, values.github_event_id(event["event_fingerprint"]))
    ):
        return False
    parent = db_records.first(
        db_read_tasks.find_for_github_followup_allows_new_subject(
            conn, parent_task_id, event["workstream_id"], values.repository_id(repo["full_name"])
        )
    )
    remote_canceled = bool(
        parent
        and parent[0] == TaskLifecycle.CANCELED.value
        and values.json_object(parent[3]).get("remote_reconciliation")
    )
    if (
        not parent
        or (parent[0] not in {TaskLifecycle.COMPLETED.value, TaskLifecycle.FAILED.value} and not remote_canceled)
        or (
            parent[1] not in {WorkstreamLifecycle.COMPLETED.value, WorkstreamLifecycle.FAILED.value}
            and not remote_canceled
        )
        or parent[2]
        or work_queries.latest_task_id(conn, event["workstream_id"]) != parent_task_id
    ):
        return False
    item = work_items.resolve_work_item_for_workstream(conn, event["workstream_id"])
    if not item or (item["canceled_at"] and (not remote_canceled or operator_canceled_item(conn, item))):
        return False
    facts = control_queries.lifecycle_facts(conn, item)
    if (
        facts["running"]
        or facts["actions"]
        or facts["cleanup_pending"]
        or facts["workflow_attention"]
        or facts["source_closed"]
    ):
        return False
    return not db_records.first(db_read_tasks.github_followup_allows_new_subject_filtered(conn, facts["streams"]))
