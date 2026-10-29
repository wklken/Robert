"""Pre-submit checks; diagnostics never reserve a final result slot."""
from contextlib import closing
import json
import sqlite3

from robert_agent import action_scope, audit_result, execution_store, remediation, route, result_contract


def context(conn, task_id, attempt_id, *, include_scope=True):
    row = conn.execute(
        """SELECT t.workstream_id,t.task_kind,t.metadata_json,t.lifecycle,
                  a.status, a.metadata_json,rd.allowed_github_actions_json,
                  rd.expected_output,rd.required_skills_json,rd.recommended_skills_json,rd.route_id
           FROM tasks t JOIN attempts a ON a.task_id=t.task_id
           JOIN route_decisions rd ON rd.task_id=t.task_id
           WHERE t.task_id=? AND a.attempt_id=?""", (task_id, attempt_id),
    ).fetchone()
    if not row:
        return None, {"ok": False, "status": "rejected_by_supervisor"}
    attempt_metadata = json.loads(row[5] or "{}")
    if attempt_metadata.get("result_contract") != result_contract.CONTRACT_ID:
        return None, {"ok": False, "status": "unsupported_contract", "reason_code": "unsupported_contract",
                      "safe_error": "This attempt has no supported submission contract; prepare a new attempt."}
    metadata = json.loads(row[2] or "{}")
    if row[3] in {"canceled", "failed", "ignored"} or row[4] not in {"prepared", "running", "stale", "completed"}:
        return None, {"ok": False, "status": "rejected_by_supervisor"}
    if json.loads(row[5] or "{}").get("result_validation"):
        return None, {"ok": False, "status": "rejected_by_supervisor"}
    subject_type, subject_id = "task", task_id
    if row[1] in audit_result.REMEDIATION_TASK_KINDS and metadata.get("remediation_episode_id"):
        subject_type, subject_id = "remediation_episode", metadata["remediation_episode_id"]
    owner = execution_store.get_binding(conn, subject_type, subject_id)
    if owner and owner["binding_status"] != "active":
        return None, {"ok": False, "status": "rejected_by_workflow_owner"}
    origin = conn.execute("SELECT origin_type FROM work_items WHERE workstream_id=?", (row[0],)).fetchone()
    episode = remediation.load_episode(conn, metadata.get("remediation_episode_id")) if row[1] in audit_result.REMEDIATION_TASK_KINDS else None
    return {
        "allowed_github_actions": json.loads(row[6]),
        "expected_output": row[7], "required_skills": json.loads(row[8] or "[]"),
        "recommended_skills": json.loads(row[9] or "[]"),
        "verification_policy": route.verification_policy_for(row[10]),
        "origin_type": origin[0] if origin else "github", "task_kind": row[1],
        "review_input_policy": metadata.get("review_input_policy", "required"),
        "remediation_context": episode,
        "action_scope": action_scope.for_result(conn, task_id, attempt_id, row[0], observe=False) if include_scope else None,
    }, None


def failure(audit):
    """Expose stable categories and locations without echoing submitted text."""
    violations = audit.get("violations", [])
    message = audit.get("safe_error", "")
    known = {"action_scope", "web_origin_github_action", "invalid_review_input_policy",
             "invalid_review_input_kind", "missing_review_point_evaluation", "invalid_review_point_evaluation",
             "missing_mixed_request_handoff", "no_implemented_review_points", "invalid_remediation_evidence",
             "remediation_evidence_mismatch", "remediation_attestation_failed"}
    code = violations[0] if violations and violations[0] in known else "result_contract"
    if violations and code == "result_contract":
        code = "action_not_allowed"
        message = "The result contains actions outside its allowed contract."
    field = "planned_github_actions"
    for name in ("consumed_work_item_event_ids", "consumed_event_fingerprints", "used_skills",
                 "operator_question", "verification", "review_point_evaluation", "review_input_kind",
                 "handoff", "recommended_route", "branch_slug", "remediation_evidence"):
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
    return {"ok": False, "status": "failed_validation", "reason_code": code,
            "field_path": field, "safe_error": message or ", ".join(violations),
            "required_action": "Correct the indicated field and submit again in this attempt."}


def _record_diagnostic(db_path, payload, reason_code):
    """Best-effort counts only; no submitted text or final-slot side effects."""
    try:
        with closing(sqlite3.connect(db_path, timeout=0.05)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT json_extract(metadata_json,'$.submission_validation_counts') FROM attempts WHERE attempt_id=? AND task_id=?", (payload.get("attempt_id"), payload.get("task_id"))).fetchone()
            if not row:
                return
            counts = json.loads(row[0] or "{}")
            counts[reason_code] = int(counts.get(reason_code, 0)) + 1
            conn.execute("UPDATE attempts SET metadata_json=json_set(metadata_json,'$.submission_validation_counts',json(?)) WHERE attempt_id=? AND task_id=?", (json.dumps(counts, sort_keys=True), payload.get("attempt_id"), payload.get("task_id")))
    except (sqlite3.Error, ValueError, TypeError):
        # A busy/unavailable diagnostics sink must not change the submission result.
        return
