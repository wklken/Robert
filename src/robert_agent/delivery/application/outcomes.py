"""application delivery outcomes responsibilities."""

from robert_agent.foundation.contracts.delivery import GitHubActionType, PublicationStatus
from robert_agent.foundation.contracts.work import REMEDIATION_TASK_KINDS

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_actions as db_read_github_actions
from robert_agent.persistence.stores import github_actions as db_write_github_actions
from robert_agent.persistence.stores import notifications as db_write_notifications
from robert_agent.workflows.review import publish as review_publish
from robert_agent.workflows.vote import publish as vote_publish
from robert_agent.workflows import pr_remediation as remediation
from robert_agent.execution import retry as retry_policy
from robert_agent.execution import operations as execution_operations
from robert_agent.persistence import execution_control as execution_store

from robert_agent.delivery.application import scope as publication_scope
from robert_agent.delivery import intents as intent_facts
from robert_agent.delivery import notifications as publication_notifications
from robert_agent.delivery import values as publication_values


def insert_workflow_attention_notification(conn, action, reason_code, now):
    notification_id = f"notification-workflow-attention-{action['action_id']}"
    metadata = {
        "action_id": action["action_id"],
        "action_type": action["action_type"],
        "reason_code": str(reason_code)[:96],
    }
    db_write_notifications.insert_workflow_attention_notification(
        conn, notification_id, action.get("task_id"), now, publication_values.encode_json(metadata)
    )


def mark_attention(conn, action, intent, scope, reconciliation, now):
    reason_code = reconciliation.get("reason_code", "publication_unknown")
    record_failure(
        conn,
        action,
        {
            "status": "publish_failed",
            "safe_error": f"Publication requires attention: {reason_code}",
            "retryable": False,
        },
        now,
    )
    if intent and intent["status"] in {
        PublicationStatus.PREPARED,
        PublicationStatus.EXECUTING,
        PublicationStatus.FAILED,
    }:
        if intent["status"] == PublicationStatus.PREPARED:
            execution_store.transition_publication_intent(
                conn, intent["publication_id"], PublicationStatus.EXECUTING.value, now
            )
        elif intent["status"] == PublicationStatus.FAILED:
            execution_store.transition_publication_intent(
                conn, intent["publication_id"], PublicationStatus.EXECUTING.value, now
            )
        execution_store.transition_publication_intent(
            conn,
            intent["publication_id"],
            PublicationStatus.ATTENTION.value,
            now,
            remote_evidence=reconciliation.get("evidence", {}),
            error={"reason_code": reason_code},
        )
    execution_operations.mark_publication_attention(conn, action.get("task_id"), scope, reason_code, now=now)
    insert_workflow_attention_notification(conn, action, reason_code, now)


def mark_skipped(conn, action, result, now):
    review_publish.record_outcome(conn, action, result.get("status"), now)
    vote_publish.record_outcome(conn, action, result.get("status"), now)
    if result.get("status") == "superseded":
        intent = intent_facts.load_intent(conn, action["action_id"])
        if intent and intent["status"] == PublicationStatus.EXECUTING:
            execution_store.transition_publication_intent(
                conn,
                intent["publication_id"],
                PublicationStatus.CONFIRMED_ABSENT.value,
                now,
                remote_evidence={"reason_code": "publication_superseded", "status": "confirmed_absent"},
            )
    metadata = publication_values.decode_json(action["metadata_json"])
    metadata["publish"] = {
        "status": "skipped",
        "reason_code": result.get("status"),
        "skipped_at": now,
        "safe_error": result.get("safe_error", ""),
    }
    db_write_github_actions.mark_skipped(conn, publication_values.encode_json(metadata), action["action_id"])
    publication_notifications.insert_notification(conn, action, "github_publish_skipped", result, now)


def skip_pending_result_actions(
    conn,
    *,
    result_id,
    excluding_action_id,
    result,
    now,
):
    rows = db_read_github_actions.find_for_skip_pending_result_actions(conn, result_id, excluding_action_id)
    for row in rows:
        mark_skipped(conn, dict(row), result, now)
    return len(rows)


def retire_unstarted_actions(conn, task_id, now):
    """Retire only actions whose durable intent proves no unknown external effect."""
    actions = db_read_github_actions.find_for_retire_unstarted_actions(conn, task_id)
    for row in actions:
        action = dict(row)
        intent = intent_facts.load_intent(conn, action["action_id"])
        if intent and intent["status"] not in {
            PublicationStatus.PREPARED,
            PublicationStatus.CONFIRMED_ABSENT,
            PublicationStatus.FAILED,
        }:
            continue
        if intent and intent["status"] != PublicationStatus.CONFIRMED_ABSENT:
            execution_store.transition_publication_intent(
                conn, intent["publication_id"], PublicationStatus.EXECUTING.value, now
            )
            execution_store.transition_publication_intent(
                conn,
                intent["publication_id"],
                PublicationStatus.CONFIRMED_ABSENT.value,
                now,
                remote_evidence={"reason_code": "source_closed_before_publication"},
            )
        mark_skipped(
            conn, action, {"status": "source_closed", "safe_error": "Source closed before publication began."}, now
        )


def record_failure(conn, action, result, now, *, reconcile_required=False):
    fresh = db_records.first(db_read_github_actions.find_metadata_json_by_action_id(conn, action["action_id"]))
    metadata = publication_values.decode_json(fresh[0] if fresh else action["metadata_json"])
    prior = dict(metadata.get("publish") or {})
    if prior.get("attempt_charged"):
        try:
            prior["attempt_count"] = max(0, int(prior.get("attempt_count", 0)) - 1)
        except (ValueError, TypeError, OverflowError):
            prior["attempt_count"] = retry_policy.MAX_ATTEMPTS
    state = retry_policy.failure_state(prior, result.get("safe_error"), now, retryable=result.get("retryable", True))
    # Authentication, permission, and branch rejections establish failure.
    # Only an ambiguous transport outcome needs a final reconciliation pass.
    reconcile_required = reconcile_required and state["error_class"] == "transient_failure"
    from robert_agent.security import redaction

    safe = redaction.redact_and_truncate(str(result.get("safe_error", "")), 2000)
    result = {
        **result,
        "safe_error": safe.get("text", "Sensitive diagnostic omitted; inspect the local log."),
        "command": [],
    }
    metadata["publish"] = {
        **state,
        "reconcile_required": bool(reconcile_required),
        "status": "publish_failed",
        "failed_at": now,
        "safe_error": result.get("safe_error", ""),
        "command": result.get("command", []),
    }
    db_write_github_actions.set_metadata_json_by_action_id(
        conn, publication_values.encode_json(metadata), action["action_id"]
    )
    publication_notifications.insert_notification(conn, action, "github_publish_failed", {**result, **state}, now)
    if not state["retryable"] and not reconcile_required:
        intent = intent_facts.load_intent(conn, action["action_id"])
        if intent and intent["status"] == PublicationStatus.EXECUTING:
            execution_store.transition_publication_intent(
                conn,
                intent["publication_id"],
                PublicationStatus.ATTENTION.value,
                now,
                error={"reason_code": state["reason_code"]},
            )
        execution_operations.mark_publication_attention(
            conn,
            action.get("task_id"),
            publication_scope.trusted_action_scope(conn, action),
            state["reason_code"],
            now=now,
        )


def record_remediation_publish_outcome(conn, action, result, now):
    if action["action_type"] != GitHubActionType.PUSH_EXISTING_PR:
        return
    action_scope = publication_scope.trusted_action_scope(conn, action)
    if not isinstance(action_scope, dict):
        return
    episode_id = action_scope.get("episode_id")
    if action_scope.get("task_kind") not in REMEDIATION_TASK_KINDS or not episode_id:
        return
    remediation.record_publish_result(
        conn,
        episode_id=episode_id,
        publish_status=result["status"],
        result_head_sha=result.get("result_head_sha"),
        now=now,
    )
