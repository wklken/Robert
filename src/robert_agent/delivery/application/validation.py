"""Pre-submit checks; diagnostics never reserve a final result slot."""

from robert_agent.foundation.contracts.execution import AttemptStatus, BindingStatus
from robert_agent.foundation.contracts.work import TaskLifecycle

from robert_agent.persistence import database as db_database
from robert_agent.persistence import errors as db_errors
from robert_agent.persistence import transactions as db_transactions

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import attempts as db_read_attempts
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import work_items as db_read_work_items
from robert_agent.persistence.stores import attempts as db_write_attempts
from contextlib import closing
import json

from robert_agent.delivery.application import scope as action_scope
from robert_agent.delivery.application import audit as audit_result
from robert_agent.persistence import execution_control as execution_store
from robert_agent.workflows import pr_remediation as remediation
from robert_agent.workflows import routes as route
from robert_agent.delivery import contract as result_contract


def context(conn, task_id, attempt_id, *, include_scope=True):
    row = db_records.first(db_read_tasks.find_for_context(conn, task_id, attempt_id))
    if not row:
        return None, {"ok": False, "status": "rejected_by_supervisor"}
    attempt_metadata = json.loads(row[5] or "{}")
    if attempt_metadata.get("result_contract") != result_contract.CONTRACT_ID:
        return None, {
            "ok": False,
            "status": "unsupported_contract",
            "reason_code": "unsupported_contract",
            "safe_error": "This attempt has no supported submission contract; prepare a new attempt.",
        }
    metadata = json.loads(row[2] or "{}")
    if row[3] in {TaskLifecycle.CANCELED.value, TaskLifecycle.FAILED.value, TaskLifecycle.IGNORED.value} or row[
        4
    ] not in {
        AttemptStatus.PREPARED.value,
        AttemptStatus.RUNNING.value,
        AttemptStatus.STALE.value,
        AttemptStatus.COMPLETED.value,
    }:
        return None, {"ok": False, "status": "rejected_by_supervisor"}
    if json.loads(row[5] or "{}").get("result_validation"):
        return None, {"ok": False, "status": "rejected_by_supervisor"}
    subject_type, subject_id = "task", task_id
    if row[1] in audit_result.REMEDIATION_TASK_KINDS and metadata.get("remediation_episode_id"):
        subject_type, subject_id = "remediation_episode", metadata["remediation_episode_id"]
    owner = execution_store.get_binding(conn, subject_type, subject_id)
    if owner and owner["binding_status"] != BindingStatus.ACTIVE:
        return None, {"ok": False, "status": "rejected_by_workflow_owner"}
    origin = db_records.first(db_read_work_items.find_origin_type_by_workstream_id(conn, row[0]))
    episode = (
        remediation.load_episode(conn, metadata.get("remediation_episode_id"))
        if row[1] in audit_result.REMEDIATION_TASK_KINDS
        else None
    )
    return {
        "allowed_github_actions": json.loads(row[6]),
        "expected_output": row[7],
        "required_skills": json.loads(row[8] or "[]"),
        "recommended_skills": json.loads(row[9] or "[]"),
        "verification_policy": route.verification_policy_for(row[10]),
        "origin_type": origin[0] if origin else "github",
        "task_kind": row[1],
        "review_input_policy": metadata.get("review_input_policy", "required"),
        "remediation_context": episode,
        "action_scope": action_scope.for_result(conn, task_id, attempt_id, row[0], observe=False)
        if include_scope
        else None,
    }, None


def failure(audit):
    """Expose stable categories and locations without echoing submitted text."""
    violations = audit.get("violations", [])
    message = audit.get("safe_error", "")
    if "uncommitted_worktree" in violations:
        return {
            "ok": False,
            "status": "failed_validation",
            "reason_code": "uncommitted_worktree",
            "field_path": "planned_github_actions",
            "safe_error": message,
            "required_action": "Commit all task-owned changes locally, preserve unrelated work, and submit again in this attempt.",
        }
    if violations and violations[0] in {"redaction_blocked", "redaction_required"}:
        return {
            "ok": False,
            "status": "failed_validation",
            "reason_code": violations[0],
            "field_path": audit.get("field_path", "planned_github_actions"),
            "findings": audit.get("findings", []),
            "safe_error": message,
            "required_action": "Redact the indicated public text and submit again in this attempt.",
        }
    known = {
        "action_scope",
        "web_origin_github_action",
        "invalid_review_input_policy",
        "invalid_review_input_kind",
        "missing_review_point_evaluation",
        "invalid_review_point_evaluation",
        "missing_mixed_request_handoff",
        "no_implemented_review_points",
        "invalid_remediation_evidence",
        "remediation_evidence_mismatch",
        "remediation_attestation_failed",
    }
    code = violations[0] if violations and violations[0] in known else "result_contract"
    if violations and code == "result_contract":
        code = "action_not_allowed"
        message = "The result contains actions outside its allowed contract."
    field = "planned_github_actions"
    for name in (
        "consumed_work_item_event_ids",
        "consumed_event_fingerprints",
        "used_skills",
        "operator_question",
        "verification",
        "review_point_evaluation",
        "review_input_kind",
        "handoff",
        "recommended_route",
        "branch_slug",
        "remediation_evidence",
    ):
        if name in message or any(name in item for item in violations):
            field = name
            code = code if code in known else "invalid_" + name
            break
    if "waiting_for_user requires" in message:
        code = "question_not_deliverable"
        field = "operator_question" if "operator_question" in message else "planned_github_actions"
    elif "before open_pr" in message or "comment after push" in message:
        code = "invalid_action_order"
    elif "missing fields" in message:
        code = "missing_action_field"
    elif "idempotency marker" in message:
        code = "missing_comment_marker"
    elif "metadata" in message:
        code = "missing_action_metadata"
    elif "expected_output" in message:
        code = "unexpected_output_type"
        field = "output_type"
    return {
        "ok": False,
        "status": "failed_validation",
        "reason_code": code,
        "field_path": field,
        "safe_error": message or ", ".join(violations),
        "required_action": "Correct the indicated field and submit again in this attempt.",
    }


def record_diagnostic(db_path, payload, reason_code):
    """Best-effort counts only; no submitted text or final-slot side effects."""
    try:
        with closing(db_database.connect(db_path, timeout=0.05)) as conn, conn:
            db_transactions.begin(conn, immediate=True)
            row = db_records.first(
                db_read_attempts.record_diagnostic(conn, payload.get("attempt_id"), payload.get("task_id"))
            )
            if not row:
                return
            counts = json.loads(row[0] or "{}")
            counts[reason_code] = int(counts.get(reason_code, 0)) + 1
            db_write_attempts.record_diagnostic(
                conn, json.dumps(counts, sort_keys=True), payload.get("attempt_id"), payload.get("task_id")
            )
    except (db_errors.Error, ValueError, TypeError):
        # A busy/unavailable diagnostics sink must not change the submission result.
        return
