"""Read worker results and persist audit and result bookkeeping."""

from robert_agent.foundation import values
from robert_agent.adapters.sqlite import review_rounds as review_store
from robert_agent.adapters.sqlite import vote_rounds as vote_store
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
    for row in conn.execute(
        """
        SELECT metadata_json, target_url, external_id
        FROM github_actions
        WHERE result_id = ?
        ORDER BY created_at, action_id
        """,
        (result_id,),
    ):
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
    conn.execute(
        """
        UPDATE github_actions
        SET audit_status = ?
        WHERE result_id = ?
        """,
        (status, result_id),
    )


def record_result_audit(conn, result_id, audit):
    row = conn.execute(
        "SELECT metadata_json FROM worker_results WHERE result_id = ?",
        (result_id,),
    ).fetchone()
    metadata = json.loads(row[0]) if row and row[0] else {}
    metadata["audit"] = {**audit, "audited_at": values.utc_now()}
    conn.execute(
        """
        UPDATE worker_results
        SET metadata_json = ?
        WHERE result_id = ?
        """,
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True), result_id),
    )


def record_result_project_memory_status(conn, result_id, status):
    row = conn.execute(
        "SELECT metadata_json FROM worker_results WHERE result_id = ?",
        (result_id,),
    ).fetchone()
    metadata = json.loads(row[0]) if row and row[0] else {}
    metadata["project_memory"] = status
    conn.execute(
        """
        UPDATE worker_results
        SET metadata_json = ?
        WHERE result_id = ?
        """,
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True), result_id),
    )


def result_actions_all_published(conn, result_id):
    rows = conn.execute(
        """
        SELECT audit_status, publish_status
        FROM github_actions
        WHERE result_id = ?
        """,
        (result_id,),
    ).fetchall()
    if not rows:
        return True
    if all(audit_status == "accepted" and publish_status == "published" for audit_status, publish_status in rows):
        return True
    if not all(
        audit_status == "accepted" and publish_status in {"published", "skipped"}
        for audit_status, publish_status in rows
    ):
        return False
    result_row = conn.execute(
        """
        SELECT metadata_json
        FROM worker_results
        WHERE result_id = ?
        """,
        (result_id,),
    ).fetchone()
    metadata = values.json_object(result_row[0] if result_row else None)
    action_scope = (metadata.get("audit") or {}).get("action_scope")
    if isinstance(action_scope, dict) and action_scope.get("vote_round_id"):
        vote = vote_store.session(conn, round_id=action_scope["vote_round_id"])
        return bool(vote and vote["status"] == "superseded")
    if isinstance(action_scope, dict) and action_scope.get("review_round_id"):
        review = review_store.session(conn, round_id=action_scope["review_round_id"])
        return bool(review and review["status"] == "superseded")
    return isinstance(action_scope, dict) and action_scope.get("task_kind") in {
        "ci_remediation",
        "merge_conflict_remediation",
    }


def result_audit_accepted(conn, result_id):
    row = conn.execute(
        "SELECT metadata_json FROM worker_results WHERE result_id = ?",
        (result_id,),
    ).fetchone()
    metadata = json.loads(row[0]) if row and row[0] else {}
    return (metadata.get("audit") or {}).get("status") == "accepted"
