"""Read-only work-item lifecycle facts shared by commands and projections."""

from robert_agent.foundation.contracts.delivery import PublicationStatus
from robert_agent.foundation.contracts.execution import BindingStatus

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


def lifecycle_facts(conn, item, *, controls=None, parked_publication_task_id=None):
    streams = related_workstreams(conn, item["workstream_id"])
    cursor = db_read_attempts.lifecycle_facts_filtered(conn, streams)
    attempts = [dict(row) for row in cursor]
    actions = [dict(row) for row in db_read_github_actions.lifecycle_facts_filtered(conn, streams)]
    task_metadata = [row[0] for row in db_read_tasks.lifecycle_facts_filtered(conn, streams)]
    subjects = set()
    for task_id, episode_id in db_read_tasks.lifecycle_facts_filtered_2(conn, streams):
        subjects.add(("task", task_id))
        if episode_id:
            subjects.add(("remediation_episode", episode_id))
    from robert_agent.persistence import execution_control as execution_store

    if controls is None:
        controls = execution_store.read_controls(conn, subjects)
    source = db_records.first(db_read_github_sources.find_state_by_source_id(conn, item["origin_source_id"]))
    pending = db_records.first(db_read_wakeups.find_for_lifecycle_facts(conn, item["work_item_id"]))
    approval_task = db_records.first(db_read_workstreams.find_for_lifecycle_facts(conn, item["workstream_id"]))
    pull_requests = [dict(row) for row in db_read_workstreams.find_related_pull_requests(conn, item["workstream_id"])]
    return lifecycle_facts_from_records(
        streams=streams,
        attempts=attempts,
        actions=actions,
        task_metadata=task_metadata,
        subjects=subjects,
        controls=controls,
        source_state=source[0] if source else None,
        publication_retry_pending=bool(pending),
        approval_task_id=approval_task[0] if approval_task else None,
        pull_requests=pull_requests,
        parked_publication_task_id=parked_publication_task_id,
    )


def lifecycle_facts_from_records(
    *,
    streams,
    attempts,
    actions,
    task_metadata,
    subjects,
    controls,
    source_state,
    publication_retry_pending,
    approval_task_id,
    pull_requests,
    parked_publication_task_id=None,
):
    """Apply the same lifecycle policy to one caller-owned read snapshot."""
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
    failed = []
    retry_blocked = False
    for action in actions:
        try:
            metadata = json.loads(action["metadata_json"] or "{}")
            publication = metadata.get("publish") or {}
        except (ValueError, AttributeError):
            publication = {}
        if action["intent_status"] == PublicationStatus.ATTENTION or (
            isinstance(publication, dict) and publication.get("retryable") is False
        ):
            retry_blocked = True
        if action["intent_status"] == PublicationStatus.ATTENTION and not publication:
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
    for encoded in task_metadata:
        try:
            metadata = json.loads(encoded or "{}")
            stopped = metadata.get("execution_attention")
        except (ValueError, AttributeError):
            stopped = {"reason_code": "invalid_task_metadata"}
        if isinstance(stopped, dict):
            attention.append(safe_text(stopped.get("reason_code") or "Execution requires operator attention."))
    for key in sorted(subjects):
        binding = controls.get(key)
        if (
            binding
            and binding["binding_status"] == BindingStatus.ATTENTION
            and key != ("task", parked_publication_task_id)
        ):
            metadata = json.loads(binding["metadata_json"] or "{}")
            attention.append(safe_text(metadata.get("reason_code") or "Execution requires operator attention."))
    open_pull_requests = [pr for pr in pull_requests if (pr["state"] or "open").lower() == "open"]
    unmerged_pull_requests = []
    for pr in pull_requests:
        if pr in open_pull_requests:
            continue
        try:
            remote = json.loads(pr["metadata_json"] or "{}").get("remote_state") or {}
            merged = isinstance(remote, dict) and bool(remote.get("merged") or remote.get("merged_at"))
        except (ValueError, AttributeError):
            merged = False
        if not merged:
            unmerged_pull_requests.append(pr)
    return {
        "pull_requests": pull_requests,
        "open_pull_requests": open_pull_requests,
        "unmerged_pull_requests": unmerged_pull_requests,
        "streams": streams,
        "running": attempts,
        "actions": actions,
        "publish_failures": failed,
        "approval_task_id": approval_task_id,
        "workflow_attention": attention,
        "publication_retry_pending": publication_retry_pending,
        "publication_retry_blocked": retry_blocked,
        "cleanup_pending": cleanup,
        "source_closed": bool((source_state or "").lower() == "closed")
        or any((a["source_state"] or "").lower() == "closed" for a in attempts),
    }


def current_task(conn, streams):
    return db_records.first(db_read_tasks.current_task_filtered(conn, streams))
