"""Claim and close execution subjects in the caller-owned transaction."""

from robert_agent.foundation.contracts.execution import BindingStatus
from robert_agent.foundation.contracts.work import (
    REMEDIATION_TASK_KINDS,
    TERMINAL_REMEDIATION_STATUSES,
    TERMINAL_TASK_LIFECYCLES,
    TaskKind,
    TaskLifecycle,
)

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import pr_remediation_episodes as db_read_pr_remediation_episodes
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import wakeups as db_read_wakeups
from robert_agent.persistence.stores import tasks as db_write_tasks
import json
from datetime import datetime, timezone
from robert_agent.persistence import execution_control as execution_store
from robert_agent.execution import operations as execution_operations
from robert_agent.execution import notifications as execution_notifications


def _json_object(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def subject_for_task(conn, task_id):
    row = db_records.first(db_read_tasks.find_task_kind_and_metadata_json_by_task_id(conn, task_id))
    episode = _json_object(row[1]).get("remediation_episode_id") if row else None
    return ("remediation_episode", episode) if episode and row[0] in REMEDIATION_TASK_KINDS else ("task", task_id)


def claim_task(conn, task_id, run_now=None):
    """Check a subject's persisted execution state before a task mutation.

    Remediation children share their Episode control and recovery budget.
    """

    row = db_records.first(db_read_tasks.find_task_kind_and_metadata_json_by_task_id(conn, task_id))
    if not row:
        return False
    run_now = run_now or datetime.now(timezone.utc).isoformat()
    task_kind, metadata_json = row
    if _json_object(metadata_json).get("execution_attention") or _json_object(metadata_json).get(
        "audit_retry_required"
    ):
        return False
    if task_kind == TaskKind.HUMAN_REQUEST:
        return (
            execution_operations.claim_subject(
                conn,
                "task",
                task_id,
                now=run_now,
            )
            is not None
        )
    if task_kind in REMEDIATION_TASK_KINDS:
        episode_id = _json_object(metadata_json).get("remediation_episode_id")
        if not episode_id:
            metadata = _json_object(metadata_json)
            if "execution_attention" not in metadata:
                metadata["execution_attention"] = {
                    "reason_code": "remediation_episode_missing",
                    "required_action": "Inspect the historical remediation task and restore its Episode association before retrying.",
                }
                db_write_tasks.set_metadata_json_by_task_id(conn, json.dumps(metadata), task_id)
                execution_notifications.insert(
                    conn,
                    "execution_attention_required",
                    "recorded",
                    run_now,
                    {"task_id": task_id, **metadata["execution_attention"]},
                )
            return False
        return (
            execution_operations.claim_subject(
                conn,
                "remediation_episode",
                episode_id,
                now=run_now,
            )
            is not None
        )
    return False


def claim_episode(conn, episode_id, run_now=None):
    if not episode_id:
        return False
    return (
        execution_operations.claim_subject(
            conn,
            "remediation_episode",
            episode_id,
            now=run_now or datetime.now(timezone.utc).isoformat(),
        )
        is not None
    )


def parent_allows_child(conn, parent_task_id, *, retrigger_wakeup_id=None):
    if not parent_task_id:
        return True
    if db_records.first(db_read_tasks.find_for_parent_allows_child(conn, parent_task_id)):
        return False
    binding = execution_store.get_binding(conn, "task", parent_task_id)
    if retrigger_wakeup_id is not None:
        # An audited operator request starts a new subject. It never reopens
        # the failed parent or changes its recorded ownership.
        requested = db_records.first(
            db_read_wakeups.find_for_parent_allows_child(conn, retrigger_wakeup_id, parent_task_id)
        )
        return bool(requested) and (
            not binding
            or (
                binding["binding_status"] == BindingStatus.TERMINAL
                or (binding["binding_status"] == BindingStatus.ACTIVE)
            )
        )
    return not binding or (binding["binding_status"] == BindingStatus.ACTIVE)


def reconcile_task_allowed(conn, task_id, run_now):
    if db_records.first(db_read_tasks.find_for_parent_allows_child(conn, task_id)):
        return False
    binding = execution_store.get_binding(conn, "task", task_id)
    if binding:
        return binding["binding_status"] in {
            BindingStatus.ACTIVE,
            BindingStatus.TERMINAL,
        }
    row = db_records.first(db_read_tasks.find_lifecycle_by_task_id(conn, task_id))
    if not row:
        return False
    if row[0] in TERMINAL_TASK_LIFECYCLES:
        return True
    return claim_task(conn, task_id, run_now)


def close_subject(conn, subject_type, subject_id, run_now):
    binding = execution_store.get_binding(conn, subject_type, subject_id)
    if binding and binding["binding_status"] == BindingStatus.ACTIVE:
        execution_store.close_binding(conn, subject_type, subject_id, run_now)


def close_terminal_task(conn, task_id, run_now):
    row = db_records.first(db_read_tasks.find_task_kind_and_lifecycle_and_metadata_json_by_task_id(conn, task_id))
    if not row:
        return
    task_kind, lifecycle, metadata_json = row
    if lifecycle not in TERMINAL_TASK_LIFECYCLES:
        return
    if task_kind == TaskKind.HUMAN_REQUEST:
        close_subject(conn, "task", task_id, run_now)
        return
    episode_id = _json_object(metadata_json).get("remediation_episode_id")
    if episode_id:
        episode = db_records.first(db_read_pr_remediation_episodes.find_status_by_episode_id(conn, episode_id))
        if episode and episode[0] in TERMINAL_REMEDIATION_STATUSES:
            close_subject(conn, "remediation_episode", episode_id, run_now)


def recovery_subject(conn, task_id):
    row = db_records.first(db_read_tasks.find_metadata_json_by_task_id(conn, task_id))
    episode_id = _json_object(row[0] if row else None).get("remediation_episode_id")
    if episode_id and execution_store.get_binding(conn, "remediation_episode", episode_id):
        return "remediation_episode", episode_id
    return "task", task_id


def mark_attention(conn, task_id, run_now):
    subject_type, subject_id = recovery_subject(conn, task_id)
    candidates = [(subject_type, subject_id)]
    if (subject_type, subject_id) != ("task", task_id):
        candidates.append(("task", task_id))
    for candidate_type, candidate_id in candidates:
        binding = execution_store.get_binding(conn, candidate_type, candidate_id)
        if binding and binding["binding_status"] == BindingStatus.ACTIVE:
            execution_store.mark_binding_attention(conn, candidate_type, candidate_id, run_now)
            return


def waiting_reply_allowed(conn, task_id, now):
    row = db_records.first(db_read_tasks.find_task_kind_and_lifecycle_and_metadata_json_by_task_id(conn, task_id))
    if not row or row[1] != TaskLifecycle.WAITING_FOR_USER.value:
        return False
    if row[0] in REMEDIATION_TASK_KINDS:
        episode = _json_object(row[2]).get("remediation_episode_id")
        state = db_records.first(
            db_read_pr_remediation_episodes.find_status_and_metadata_json_by_episode_id(conn, episode)
        )
        if (
            state
            and state[0] == "exhausted"
            and _json_object(state[1]).get("last_transition_reason") == "worker_waiting_for_user"
        ):
            return True  # The trusted reply owns a fresh human subject, not the ended Episode.
    return claim_task(conn, task_id, now)
