"""Read worker results and persist audit and result bookkeeping."""

from robert_agent.foundation.contracts.delivery import AuditStatus, PublishStatus
from robert_agent.foundation.contracts.work import REMEDIATION_TASK_KINDS

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_actions as db_read_github_actions
from robert_agent.persistence.queries import worker_results as db_read_worker_results
from robert_agent.persistence.stores import github_actions as db_write_github_actions
from robert_agent.persistence.stores import worker_results as db_write_worker_results
from robert_agent.foundation import values
from robert_agent.persistence.rounds import review as review_store
from robert_agent.persistence.rounds import vote as vote_store
import json


def load_result_payload(conn, result_row):
    (
        result_id,
        task_id,
        attempt_id,
        output_type,
        consumed_json,
        verification_json,
        handoff,
        metadata_json,
    ) = result_row
    actions = []
    for row in db_read_github_actions.find_metadata_json_and_target_url_and_external_id_by_result_id(conn, result_id):
        action = json.loads(row[0])
        if row[1]:
            action["target_url"] = row[1]
            action["url"] = row[1]
        if row[2]:
            action["id"] = row[2]
        actions.append(action)
    metadata = json.loads(metadata_json) if metadata_json else {}
    recommended_route = metadata.get("recommended_route", "")
    return {
        "result_id": result_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "output_type": output_type,
        "planned_github_actions": actions,
        "consumed_event_fingerprints": json.loads(consumed_json),
        "verification": json.loads(verification_json),
        "handoff": handoff,
        "used_skills": metadata.get("used_skills", []),
        "memory_delta": metadata.get("memory_delta"),
        "used_memory_ids": metadata.get("used_memory_ids", []),
        "review_point_evaluation": metadata.get("review_point_evaluation", []),
        "review_input_kind": metadata.get("review_input_kind"),
        "recommended_route": recommended_route,
        "branch_slug": metadata.get("branch_slug", ""),
        "consumed_work_item_event_ids": metadata.get(
            "consumed_work_item_event_ids",
            [],
        ),
        "submission_head_sha": metadata.get("submission_head_sha"),
        "operator_question": metadata.get("operator_question"),
        "remediation_evidence": metadata.get("remediation_evidence"),
    }


def set_github_actions_status(conn, result_id, status):
    db_write_github_actions.set_audit_status_by_result_id(conn, status, result_id)


def record_result_audit(conn, result_id, audit):
    row = db_records.first(db_read_worker_results.find_metadata_json_by_result_id(conn, result_id))
    metadata = json.loads(row[0]) if row and row[0] else {}
    metadata["audit"] = {**audit, "audited_at": values.utc_now()}
    db_write_worker_results.set_metadata_json_by_result_id(
        conn, json.dumps(metadata, ensure_ascii=False, sort_keys=True), result_id
    )


def record_result_project_memory_status(conn, result_id, status):
    row = db_records.first(db_read_worker_results.find_metadata_json_by_result_id(conn, result_id))
    metadata = json.loads(row[0]) if row and row[0] else {}
    metadata["project_memory"] = status
    db_write_worker_results.set_metadata_json_by_result_id(
        conn, json.dumps(metadata, ensure_ascii=False, sort_keys=True), result_id
    )


def result_actions_all_published(conn, result_id):
    rows = db_read_github_actions.find_audit_status_and_publish_status_by_result_id(conn, result_id)
    if not rows:
        return True
    if all(
        audit_status == AuditStatus.ACCEPTED and publish_status == PublishStatus.PUBLISHED
        for audit_status, publish_status in rows
    ):
        return True
    if not all(
        audit_status == AuditStatus.ACCEPTED and publish_status in {PublishStatus.PUBLISHED, PublishStatus.SKIPPED}
        for audit_status, publish_status in rows
    ):
        return False
    result_row = db_records.first(db_read_worker_results.find_metadata_json_by_result_id(conn, result_id))
    metadata = values.json_object(result_row[0] if result_row else None)
    action_scope = (metadata.get("audit") or {}).get("action_scope")
    if isinstance(action_scope, dict) and action_scope.get("vote_round_id"):
        vote = vote_store.session(conn, round_id=action_scope["vote_round_id"])
        return bool(vote and vote["status"] == "superseded")
    if isinstance(action_scope, dict) and action_scope.get("review_round_id"):
        review = review_store.session(conn, round_id=action_scope["review_round_id"])
        return bool(review and review["status"] == "superseded")
    return isinstance(action_scope, dict) and action_scope.get("task_kind") in REMEDIATION_TASK_KINDS


def result_audit_accepted(conn, result_id):
    row = db_records.first(db_read_worker_results.find_metadata_json_by_result_id(conn, result_id))
    metadata = json.loads(row[0]) if row and row[0] else {}
    return (metadata.get("audit") or {}).get("status") == "accepted"
