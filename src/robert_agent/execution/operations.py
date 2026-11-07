"""SQLite subject state, process identity and domain recovery operations."""

from robert_agent.foundation.contracts.execution import AttemptStatus, BindingStatus
from robert_agent.foundation.contracts.work import (
    FactClassification,
    REMEDIATION_TASK_KINDS,
    TERMINAL_REMEDIATION_STATUSES,
    TERMINAL_TASK_LIFECYCLES,
    TaskKind,
)

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import pr_remediation_episodes as db_read_pr_remediation_episodes
from robert_agent.persistence.queries import tasks as db_read_tasks

from robert_agent.persistence import database as db_database
from robert_agent.persistence import errors as db_errors

from robert_agent.persistence.queries import attempts as db_read_attempts
from robert_agent.persistence.queries import execution_fact_receipts as db_read_execution_fact_receipts
from robert_agent.persistence.stores import attempts as db_write_attempts
from robert_agent.persistence.stores import execution_fact_receipts as db_write_execution_fact_receipts
from robert_agent.persistence.stores import execution_subjects as db_write_execution_subjects
from robert_agent.persistence.stores import notifications as db_write_notifications

import json
from contextlib import closing
from datetime import datetime, timezone
from robert_agent.adapters.processes import identity as worker_identity
from robert_agent.execution import retry as retry_policy
from robert_agent.persistence import execution_control as execution_store

_SENSITIVE_METADATA_KEYS = {
    "api_key",
    "authorization",
    "credential",
    "credentials",
    "database_url",
    "password",
    "private_key",
    "secret",
    "system_database_url",
    "token",
}


def _json_object(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_metadata(value):
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.lower().replace("-", "_")
            if normalized in _SENSITIVE_METADATA_KEYS or any(
                token in normalized
                for token in (
                    "credential",
                    "password",
                    "secret",
                    "token",
                    "private_key",
                )
            ):
                continue
            safe[key_text] = _safe_metadata(item)
        return safe
    if isinstance(value, list):
        return [_safe_metadata(item) for item in value]
    return value


def _validate_subject_type(subject_type):
    if subject_type not in {"task", "remediation_episode"}:
        raise ValueError("invalid workflow subject type")


def _load_episode(conn, episode_id):
    row = db_records.first(db_read_pr_remediation_episodes.find_record_by_episode_id(conn, episode_id))
    if row is None:
        raise ValueError("workflow remediation episode subject does not exist")
    return row


def _existing_active_binding(conn, subject_type, subject_id):
    binding = execution_store.get_binding(conn, subject_type, subject_id)
    if not binding:
        return None
    if binding["binding_status"] == BindingStatus.ACTIVE:
        return binding
    return False


def _claim(conn, subject_type, subject_id, workflow_kind, eligible, now):
    existing = _existing_active_binding(conn, subject_type, subject_id)
    if existing is not None:
        return existing if existing is not False else None
    if not eligible:
        return None
    try:
        return execution_store.ensure_subject(
            conn,
            subject_type,
            subject_id,
            workflow_kind,
            now,
        )
    except db_errors.IntegrityError:
        # A concurrent claim wins; never reset existing execution control.
        existing = execution_store.get_binding(conn, subject_type, subject_id)
        if existing and existing["binding_status"] == BindingStatus.ACTIVE:
            return existing
        return None


def claim_subject(conn, subject_type, subject_id, *, now):
    """Claim an eligible subject without reopening stopped execution.

    The caller owns the transaction. An active control may still be returned
    for a terminal domain row while crash recovery finishes; an unbound terminal
    row cannot be newly claimed.
    """

    _validate_subject_type(subject_type)
    if subject_type == "task":
        task = db_records.first(db_read_tasks.find_task_kind_and_lifecycle_by_task_id(conn, subject_id))
        if task is None:
            raise ValueError("workflow task subject does not exist")
        task_kind, lifecycle = task
        if task_kind != TaskKind.HUMAN_REQUEST:
            raise ValueError("workflow task subject is not an eligible human task")
        return _claim(
            conn,
            "task",
            subject_id,
            TaskKind.HUMAN_REQUEST.value,
            lifecycle not in TERMINAL_TASK_LIFECYCLES,
            now,
        )

    episode = _load_episode(conn, subject_id)
    episode_status = episode["status"] if hasattr(episode, "keys") else episode[7]
    return _claim(
        conn,
        "remediation_episode",
        subject_id,
        "pr_remediation",
        episode_status not in TERMINAL_REMEDIATION_STATUSES,
        now,
    )


def _attempt_dispatch_identity(conn, attempt_id):
    row = db_records.first(db_read_attempts.find_task_id_and_status_and_metadata_json_by_attempt_id(conn, attempt_id))
    if row is None:
        return None
    task_id, status, metadata_json = row
    metadata = _json_object(metadata_json)
    dispatch = metadata.get("dispatch")
    if not isinstance(dispatch, dict):
        dispatch = {}
    return {
        "task_id": task_id,
        "status": status,
        "metadata_json": metadata_json,
        "dispatch": dispatch,
    }


def terminate_worker(db_path, attempt_id):
    """Terminate and durably record one recorded Worker's outcome."""

    now = datetime.now(timezone.utc).isoformat()
    with closing(db_database.connect(db_path)) as conn, conn:
        attempt = _attempt_dispatch_identity(conn, attempt_id)
        if not attempt:
            return "attention"
        try:
            result = worker_identity.terminate_recorded_identity(attempt["dispatch"])
        except Exception as exc:  # pragma: no cover - defensive seam boundary
            result = {
                "status": "attention",
                "reason": "termination_failed",
                "error_type": type(exc).__name__,
            }
        if not isinstance(result, dict):
            result = {"status": "attention", "reason": "invalid_result"}
        result = _safe_metadata(result)
        status = result.get("status", "attention")
        if status not in {"terminated", "already_dead", "attention"}:
            status = "attention"
            result = {**result, "status": status}

        metadata = _json_object(attempt.get("metadata_json"))
        from robert_agent.execution.rounds import terminate_children

        children_status = terminate_children(conn, attempt["task_id"])
        if children_status == "attention":
            status = "attention"
            result = {**result, "status": status, "reason": "review_child_identity_unknown"}
        metadata["termination"] = result
        db_write_attempts.set_metadata_json_by_attempt_id(
            conn, json.dumps(metadata, ensure_ascii=False, sort_keys=True), attempt_id
        )

        if status == "attention":
            task_row = db_records.first(
                db_read_tasks.find_task_kind_and_metadata_json_by_task_id(conn, attempt["task_id"])
            )
            subject_candidates = []
            if task_row:
                task_metadata = _json_object(task_row[1])
                episode_id = task_metadata.get("remediation_episode_id")
                if episode_id:
                    subject_candidates.append(("remediation_episode", episode_id))
            subject_candidates.append(("task", attempt["task_id"]))
            for subject_type, subject_id in subject_candidates:
                binding = execution_store.get_binding(conn, subject_type, subject_id)
                if binding and binding["binding_status"] == BindingStatus.ACTIVE:
                    execution_store.mark_binding_attention(conn, subject_type, subject_id, now)
                    break

        notification_metadata = {
            "attempt_id": attempt_id,
            "task_id": attempt["task_id"],
            "termination": result,
            "status": status,
        }
        db_write_notifications.insert_for_terminate_worker(
            conn,
            f"notification-worker-termination-{attempt_id}",
            attempt["task_id"],
            now,
            json.dumps(notification_metadata, ensure_ascii=False, sort_keys=True),
        )
        return status


def _recovery_identity_status(evidence):
    if not isinstance(evidence, dict):
        return None
    for key in ("identity_status", "worker_identity_status", "process_status"):
        value = evidence.get(key)
        if isinstance(value, dict):
            value = value.get("status")
        if value:
            if value in {"not_found", "dead"}:
                return "dead"
            if value in {"alive", "alive_same"}:
                return "alive_same"
            if value in {"unknown", "attention"}:
                return "unknown"
    identity = evidence.get("identity")
    if isinstance(identity, dict):
        value = identity.get("status")
        if value in {"not_found", "dead"}:
            return "dead"
        if value in {"alive", "alive_same"}:
            return "alive_same"
        if value in {"unknown", "attention"}:
            return "unknown"
    return None


def _mark_recovery_attention(conn, binding, now):
    if not binding:
        return
    if binding["binding_status"] in {BindingStatus.ACTIVE, BindingStatus.ATTENTION}:
        execution_store.mark_binding_attention(
            conn,
            binding["subject_type"],
            binding["subject_id"],
            now,
        )


def _attempt_matches_subject(conn, subject_type, subject_id, attempt_id, task_id):
    if subject_type == "task":
        return task_id == subject_id
    if subject_type != "remediation_episode":
        return False
    row = db_records.first(db_read_tasks.find_metadata_json_and_workstream_id_by_task_id(conn, task_id))
    episode = db_records.first(
        db_read_pr_remediation_episodes.find_workstream_id_and_last_task_id_by_episode_id(conn, subject_id)
    )
    if not row or not episode:
        return False
    metadata = _json_object(row[0])
    if metadata.get("remediation_episode_id") == subject_id:
        return True
    return row[1] == episode[0] and episode[1] == task_id


def classify_recovery(conn, subject_type, subject_id, attempt_id, evidence, *, now):
    """Classify one dead Worker recovery under the binding's one-shot quota.

    The caller owns the transaction.  No Attempt is created here; the caller
    must create it only after receiving ``recover_once``.
    """

    try:
        _validate_subject_type(subject_type)
    except ValueError:
        return "attention"
    binding = execution_store.get_binding(conn, subject_type, subject_id)
    attempt = _attempt_dispatch_identity(conn, attempt_id)
    identity_status = _recovery_identity_status(evidence)
    logs = evidence.get("logs", {}) if isinstance(evidence, dict) else {}
    terminal_error = retry_policy.worker_error(logs)
    rate_limit = retry_policy.terminal_rate_limit(logs)
    category = "rate_limited" if rate_limit else retry_policy.error_class(terminal_error)
    if category not in {"transient_failure", "rate_limited"}:
        _mark_recovery_attention(conn, binding, now)
        if binding:
            db_write_execution_subjects.update_for_dispatch_queued_locked(
                conn,
                json.dumps(
                    {
                        **_json_object(binding.get("metadata_json")),
                        "reason_code": category,
                        "required_action": "Repair the reported cause, then retry the task.",
                    }
                ),
                subject_type,
                subject_id,
            )
        evidence["reason_code"] = category
        return "attention"
    valid_progress = (
        isinstance(evidence, dict)
        and evidence.get("status") == "recoverable_progress"
        and evidence.get("task_id")
        and evidence.get("previous_attempt_id") == attempt_id
        and isinstance(evidence.get("progress_signals"), list)
        and bool(evidence.get("progress_signals"))
    )
    # A structured terminal transport interruption may be retried before any
    # work was saved. It consumes the same one-shot recovery budget.
    valid_progress = valid_progress or bool(
        isinstance(evidence, dict)
        and evidence.get("status") == "transport_interrupted"
        and evidence.get("transport_failure")
        and evidence.get("task_id")
        and evidence.get("previous_attempt_id") == attempt_id
    )
    valid_progress = valid_progress or bool(
        isinstance(evidence, dict)
        and evidence.get("status") == "rate_limited"
        and rate_limit
        and evidence.get("model_fallback")
        and evidence.get("task_id")
        and evidence.get("previous_attempt_id") == attempt_id
    )
    consistent_attempt = (
        bool(
            attempt
            and attempt["status"] in {AttemptStatus.RUNNING, AttemptStatus.STALE, AttemptStatus.FAILED}
            and _attempt_matches_subject(
                conn,
                subject_type,
                subject_id,
                attempt_id,
                attempt["task_id"],
            )
            and evidence.get("task_id") == attempt["task_id"]
        )
        if isinstance(evidence, dict)
        else False
    )
    owner_ok = bool(binding and binding["binding_status"] == BindingStatus.ACTIVE)
    if not owner_ok or not consistent_attempt:
        _mark_recovery_attention(conn, binding, now)
        return "attention"
    if identity_status == "alive_same":
        return "continue"
    if identity_status != "dead" or not valid_progress:
        _mark_recovery_attention(conn, binding, now)
        return "attention"
    updated = db_write_execution_subjects.update_for_classify_recovery(conn, now, subject_type, subject_id)
    if updated != 1:
        _mark_recovery_attention(conn, binding, now)
        return "attention"
    return "recover_once"


def publication_subject_for_action(conn, task_id, action_scope):
    """Resolve the workflow subject for a publication without guessing.

    Remediation actions carry their Episode identity in the accepted audit
    scope.  All other actions belong to their ordinary human-request Task.
    The helper intentionally returns a missing binding as ``None``; legacy
    actions remain protected by their intent without fabricating ownership.
    """

    scope = action_scope if isinstance(action_scope, dict) else {}
    task_kind = scope.get("task_kind")
    episode_id = scope.get("episode_id")
    if task_kind in REMEDIATION_TASK_KINDS and episode_id:
        subject_type, subject_id = "remediation_episode", str(episode_id)
    else:
        subject_type, subject_id = "task", str(task_id or "")
    if not subject_id:
        return None, None, None
    binding = execution_store.get_binding(conn, subject_type, subject_id)
    return subject_type, subject_id, binding


def mark_publication_attention(conn, task_id, action_scope, reason_code, *, now):
    """Mark an active owning binding Attention and return its subject.

    This operation is caller-transactional and never reopens terminal or
    already-attention bindings.  A missing binding is a valid legacy state.
    """

    subject_type, subject_id, binding = publication_subject_for_action(conn, task_id, action_scope)
    if binding and binding.get("binding_status") == BindingStatus.ACTIVE:
        execution_store.mark_binding_attention(conn, subject_type, subject_id, now)
    return subject_type, subject_id, binding


def publication_scope_is_current(conn, task_id, action_scope):
    """Return whether an accepted publication scope may safely retry absence."""

    subject_type, subject_id, binding = publication_subject_for_action(conn, task_id, action_scope)
    if binding and binding.get("binding_status") != BindingStatus.ACTIVE:
        return False
    # Legacy rows may predate workflow bindings.  Their durable publication
    # intent still protects the action; without a binding there is no owner to
    # reopen or transfer, so the authoritative action/task row is sufficient
    # for an absence retry.
    if binding is None:
        return True
    if subject_type == "remediation_episode":
        row = db_records.first(db_read_pr_remediation_episodes.find_status_by_episode_id(conn, subject_id))
        return bool(row and row[0] not in TERMINAL_REMEDIATION_STATUSES)
    row = db_records.first(db_read_tasks.find_lifecycle_by_task_id(conn, subject_id))
    return bool(row and row[0] not in TERMINAL_TASK_LIFECYCLES)


def bound_attempt_for_subject(conn, subject_type, subject_id):
    """Return the one mutable Attempt attached to an exact workflow subject."""

    if subject_type == "task":
        row = db_records.first(db_read_attempts.find_for_bound_attempt_for_subject(conn, subject_id))
        return row[0] if row else None
    if subject_type != "remediation_episode":
        raise ValueError("unknown workflow subject type")
    row = db_records.first(db_read_pr_remediation_episodes.find_last_task_id_by_episode_id(conn, subject_id))
    if not row or not row[0]:
        return None
    task_id = row[0]
    attempt = db_records.first(db_read_attempts.find_for_bound_attempt_for_subject(conn, task_id))
    return attempt[0] if attempt else None


def apply_classified_fact(conn, subject_type, subject_id, fact, *, now):
    """Apply a classified fact and its dedupe receipt in the caller transaction.

    Bounded evidence is persisted with the execution state. Historical fact
    replay returns its receipt without replacing newer evidence.
    """

    from robert_agent.work import events as workflow_events

    binding = execution_store.get_binding(conn, subject_type, subject_id)
    if not binding:
        raise ValueError("workflow subject is not bound")
    classification, evidence = workflow_events.classify_and_normalize(fact)
    if evidence["subject_type"] != subject_type or evidence["subject_id"] != subject_id:
        raise ValueError("workflow fact subject does not match its binding")
    metadata = _json_object(binding.get("metadata_json"))
    latest = metadata.get("latest_fact")
    if isinstance(latest, dict) and latest.get("fact_id"):
        db_write_execution_fact_receipts.record_historical_facts(
            conn, subject_type, subject_id, latest["fact_id"], json.dumps(latest, sort_keys=True)
        )
    recorded = db_records.first(
        db_read_execution_fact_receipts.find_for_apply_classified_fact(
            conn, subject_type, subject_id, evidence["fact_id"]
        )
    )
    if recorded:
        if recorded[0] is not None and json.loads(recorded[0]) != evidence:
            raise ValueError("workflow fact id conflicts with recorded evidence")
        return {
            "classification": classification,
            "fact_id": evidence["fact_id"],
            "evidence": latest if isinstance(latest, dict) else evidence,
            "duplicate": True,
            "binding_status": binding["binding_status"],
            "attempt_id": bound_attempt_for_subject(conn, subject_type, subject_id),
        }
    if isinstance(latest, dict) and latest.get("fact_id") == evidence["fact_id"]:
        if latest != evidence:
            raise ValueError("workflow fact id conflicts with recorded evidence")
        return {
            "classification": classification,
            "fact_id": evidence["fact_id"],
            "evidence": evidence,
            "duplicate": True,
            "binding_status": binding["binding_status"],
            "attempt_id": bound_attempt_for_subject(conn, subject_type, subject_id),
        }
    if binding["binding_status"] != BindingStatus.ACTIVE:
        raise ValueError("workflow subject binding is not active")

    db_write_execution_fact_receipts.insert_for_apply_classified_fact(
        conn, subject_type, subject_id, evidence["fact_id"], json.dumps(evidence, sort_keys=True)
    )
    metadata["latest_fact"] = evidence
    db_write_execution_subjects.update_for_apply_classified_fact(
        conn, json.dumps(metadata, ensure_ascii=False, sort_keys=True), now, subject_type, subject_id
    )

    if classification == FactClassification.TERMINAL.value:
        execution_store.close_binding(conn, subject_type, subject_id, now)
    elif classification == FactClassification.INVALIDATING.value:
        execution_store.mark_binding_attention(conn, subject_type, subject_id, now)

    return {
        "classification": classification,
        "fact_id": evidence["fact_id"],
        "evidence": evidence,
        "duplicate": False,
        "binding_status": (
            BindingStatus.TERMINAL.value
            if classification == FactClassification.TERMINAL.value
            else BindingStatus.ATTENTION.value
            if classification == FactClassification.INVALIDATING.value
            else BindingStatus.ACTIVE.value
        ),
        "attempt_id": bound_attempt_for_subject(conn, subject_type, subject_id),
    }
