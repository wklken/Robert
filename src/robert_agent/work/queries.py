"""Read task and source facts without importing the runtime driver."""

from robert_agent.foundation.contracts.delivery import GitHubActionType, PublishStatus
from robert_agent.foundation.contracts.work import SourceProvider
from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import attempts as db_read_attempts
from robert_agent.persistence.queries import github_actions as db_read_github_actions
from robert_agent.persistence.queries import github_sources as db_read_github_sources
from robert_agent.persistence.queries import publication_intents as db_read_publication_intents
from robert_agent.persistence.queries import task_events as db_read_task_events
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import workstreams as db_read_workstreams
from robert_agent.persistence.queries import worker_results as db_read_worker_results
import json


def waiting_delivery_context(conn, task_id):
    """Find an unfinished delivery through analysis-only continuation ancestry."""
    seen = set()
    workstream_id = None
    while task_id and task_id not in seen:
        seen.add(task_id)
        row = db_records.first(db_read_tasks.find_record_by_task_id(conn, task_id))
        if not row:
            return None
        task = dict(row)
        if workstream_id is None:
            workstream_id = task["workstream_id"]
        if task["workstream_id"] != workstream_id or task["lifecycle"] not in {"waiting_for_user", "completed"}:
            return None
        if task["route_id"] in {"new-pr", "update-existing-pr"}:
            attempt = db_records.first(db_read_attempts.find_for_latest_attempt(conn, task_id))
            if not attempt or attempt[1] != "completed":
                return None
            results = db_read_worker_results.find_for_results_for_task(conn, task_id, 1)
            result = dict(results[0]) if results else None
            if (
                not result
                or result["attempt_id"] != attempt[0]
                or result["output_type"] != "waiting_for_user"
                or json.loads(result["metadata_json"]).get("audit", {}).get("status") != "accepted"
            ):
                return None
            metadata = json.loads(task["metadata_json"])
            return {
                **task,
                "base_branch": metadata.get("target_base_branch"),
                "previous_attempt_id": attempt[0],
                "branch_name": attempt[2],
                "worktree_path": attempt[3],
                "handoff": result["handoff"],
                "operator_question": json.loads(result["metadata_json"]).get("operator_question"),
            }
        if task["route_id"] != "comment-analysis":
            return None
        task_id = task["parent_task_id"]
    return None


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


def tapd_reply_in_attention(conn, workstream_id, task_id, ticket_url):
    """A parked ticket reply is the task's only unfinished effect and owns no Worker slot."""
    if not ticket_url or not db_records.first(
        db_read_publication_intents.find_reply_in_attention(conn, task_id, ticket_url)
    ):
        return False
    unfinished = [
        (action_type, target_url)
        for _, action_type, target_url, _, _, status, _ in db_read_github_actions.find_for_actions_for_task(
            conn, task_id
        )
        if status == PublishStatus.NOT_PUBLISHED
    ]
    return unfinished == [(GitHubActionType.COMMENT, ticket_url)] and not any(
        row[1] == task_id for row in db_read_attempts.lifecycle_facts_filtered(conn, [workstream_id])
    )


def remote_source_rows(conn, repo_id):
    return db_read_workstreams.find_for_remote_source_rows(conn, repo_id)


def tapd_origin(conn, workstream_id):
    row = db_records.first(db_read_github_sources.find_tapd_origin_for_workstream(conn, workstream_id))
    if row:
        return dict(zip(("project_key", "external_id", "url", "source_type", "state", "pr_number", "repo"), row))


def tapd_pull_requests(conn, ticket_workstream_id):
    return [
        {"number": number, "url": url, "state": state}
        for number, url, state in db_read_workstreams.find_recent_derived_pull_requests(conn, ticket_workstream_id, 10)
    ]


def tapd_discussions(conn, workstream_id):
    """Return the stored ticket snapshot of each TAPD source in the workstream."""
    return [
        {**(json.loads(metadata_json or "{}").get("tapd") or {}), "url": html_url}
        for html_url, metadata_json in db_read_github_sources.find_tapd_snapshots_for_workstream(conn, workstream_id)
    ]


def task_events(conn, task_id):
    events = [json.loads(payload or "{}") for (payload,) in db_read_task_events.find_for_for_result(conn, task_id)]
    row = db_records.first(db_read_tasks.find_metadata_json_by_task_id(conn, task_id))
    trigger = json.loads(row[0] or "{}").get("request_event_fingerprint") if row else None
    if trigger:
        events.sort(key=lambda event: event.get("event_fingerprint") != trigger)
    return events


def tapd_driven(events):
    return bool(events and events[0].get("provider") == SourceProvider.TAPD)


def sources_for_task(conn, workstream_id, events):
    """A TAPD request answers only on TAPD; any other task never sees a TAPD source."""
    tapd = tapd_driven(events)
    return [
        {"source_type": source_type, "number": number}
        if provider == SourceProvider.GITHUB
        else {"source_type": source_type, "provider": provider, "project_key": project_key, "external_id": external_id}
        for source_type, number, provider, project_key, external_id in (
            db_read_github_sources.find_for_sources_for_workstream(conn, workstream_id, workstream_id)
        )
        if (provider == SourceProvider.TAPD) == tapd
    ]
