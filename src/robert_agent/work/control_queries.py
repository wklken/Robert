"""Read-only work-item lifecycle facts shared by commands and projections."""

from robert_agent.persistence.queries import attempts as db_read_attempts
from robert_agent.persistence.queries import github_actions as db_read_github_actions

from robert_agent.persistence.queries import tasks as db_read_tasks

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_sources as db_read_github_sources
from robert_agent.persistence.queries import wakeups as db_read_wakeups
from robert_agent.persistence.queries import workstreams as db_read_workstreams
import json
from robert_agent.security.text import safe_text


def related_workstreams(conn, workstream_id):
    return [row[0] for row in db_read_workstreams.find_for_related_workstreams(conn, workstream_id, workstream_id)]


def lifecycle_facts(conn, item, *, controls=None):
    streams = related_workstreams(conn, item["workstream_id"])
    cursor = db_read_attempts.lifecycle_facts_filtered(conn, streams)
    attempts = [dict(row) for row in cursor]
    cleanup = []
    for attempt in attempts:
        try:
            metadata = json.loads(attempt["metadata_json"] or "{}")
            termination = metadata.get("termination") or {}
            supervision = metadata.get("supervise") or {}
            if isinstance(supervision, dict) and supervision.get("status") == "worker_identity_attention":
                cleanup.append(
                    safe_text(
                        supervision.get("reason")
                        or "Automatic recovery stopped: verify worker identity or recovery quota, then cancel this execution."
                    )
                )
        except (ValueError, AttributeError):
            termination = {}
        if isinstance(termination, dict) and termination.get("status") not in {
            None,
            "terminated",
            "not_found",
            "already_dead",
        }:
            cleanup.append(safe_text(termination.get("reason") or termination.get("status")))
    cursor = db_read_github_actions.lifecycle_facts_filtered(conn, streams)
    actions = [dict(row) for row in cursor]
    failed = []
    retry_blocked = False
    for action in actions:
        try:
            metadata = json.loads(action["metadata_json"] or "{}")
            publication = metadata.get("publish") or {}
        except (ValueError, AttributeError):
            publication = {}
        if action["intent_status"] == "attention" or (
            isinstance(publication, dict) and publication.get("retryable") is False
        ):
            retry_blocked = True
        if action["intent_status"] == "attention" and not publication:
            publication = {
                "status": "publish_failed",
                "safe_error": "Publication reconciliation requires operator attention.",
            }
        if isinstance(publication, dict) and publication.get("status") == "publish_failed":
            failed.append(
                {
                    "action_id": action["action_id"],
                    "type": action["action_type"],
                    "reason": safe_text(publication.get("safe_error"))
                    + " "
                    + safe_text(
                        publication.get("required_action")
                        or (("Next retry: " + publication["next_retry_at"]) if publication.get("next_retry_at") else "")
                    ),
                    "failed_at": publication.get("failed_at"),
                    **{
                        key: publication.get(key)
                        for key in (
                            "reason_code",
                            "attempt_count",
                            "max_attempts",
                            "retryable",
                            "next_retry_at",
                            "required_action",
                        )
                    },
                }
            )
    attention = []
    for (encoded,) in db_read_tasks.lifecycle_facts_filtered(conn, streams):
        stopped = json.loads(encoded or "{}").get("execution_attention")
        if isinstance(stopped, dict):
            attention.append(safe_text(stopped.get("reason_code") or "Execution requires operator attention."))
    from robert_agent.persistence import execution_control as execution_store

    subjects = set()
    for task_id, episode_id in db_read_tasks.lifecycle_facts_filtered_2(conn, streams):
        subjects.add(("task", task_id))
        if episode_id:
            subjects.add(("remediation_episode", episode_id))
    if controls is None:
        controls = execution_store.read_controls(conn, subjects)
    for key in sorted(subjects):
        binding = controls.get(key)
        if binding and binding["binding_status"] == "attention":
            metadata = json.loads(binding["metadata_json"] or "{}")
            attention.append(safe_text(metadata.get("reason_code") or "Execution requires operator attention."))
    source = db_records.first(db_read_github_sources.find_state_by_source_id(conn, item["origin_source_id"]))
    pending = db_records.first(db_read_wakeups.find_for_lifecycle_facts(conn, item["work_item_id"]))
    approval_task = db_records.first(db_read_workstreams.find_for_lifecycle_facts(conn, item["workstream_id"]))
    return {
        "streams": streams,
        "running": attempts,
        "actions": actions,
        "publish_failures": failed,
        "approval_task_id": approval_task[0] if approval_task else None,
        "workflow_attention": attention,
        "publication_retry_pending": bool(pending),
        "publication_retry_blocked": retry_blocked,
        "cleanup_pending": cleanup,
        "source_closed": bool(source and (source[0] or "").lower() == "closed")
        or any((a["source_state"] or "").lower() == "closed" for a in attempts),
    }


def current_task(conn, streams):
    return db_records.first(db_read_tasks.current_task_filtered(conn, streams))
