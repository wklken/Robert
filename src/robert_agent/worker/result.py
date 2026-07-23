#!/usr/bin/env python3
import argparse
from contextlib import closing
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from robert_agent.common import emit


def _insert_worker_result_wakeup(conn, payload, result_id, created_at):
    repo = conn.execute(
        """
        SELECT w.repo_id, wi.work_item_id
        FROM tasks t
        JOIN workstreams w ON w.workstream_id = t.workstream_id
        LEFT JOIN work_items wi ON wi.workstream_id = t.workstream_id
        WHERE t.task_id = ?
        """,
        (payload["task_id"],),
    ).fetchone()
    if not repo:
        return None
    wakeup_id = f"wakeup-{uuid4().hex[:12]}"
    metadata = {
        "recorded_by": "robert.worker",
        "output_type": payload.get("output_type"),
    }
    conn.execute(
        """
        INSERT OR IGNORE INTO wakeups(
          wakeup_id, repo_id, reason, dedupe_key, work_item_id, task_id, attempt_id,
          result_id, status, not_before_at, created_at, updated_at, metadata_json
        )
        VALUES (?, ?, 'worker_result_ready', ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
        """,
        (
            wakeup_id,
            repo[0],
            result_id,
            repo[1],
            payload["task_id"],
            payload["attempt_id"],
            result_id,
            created_at,
            created_at,
            created_at,
            json.dumps(metadata, sort_keys=True),
        ),
    )
    return wakeup_id


def _validate_actions(actions):
    if not isinstance(actions, list):
        return None, "planned_github_actions must be a list"
    for action in actions:
        if not isinstance(action, dict):
            return None, "planned_github_actions entries must be objects"
        action_type = action.get("type")
        if not isinstance(action_type, str) or not action_type:
            return None, "planned_github_actions[*].type must be a non-empty string"
    return actions, None


def _validate_used_skills(used_skills):
    if not isinstance(used_skills, list):
        return None, "used_skills must be a list"
    for skill in used_skills:
        if not isinstance(skill, str) or not skill:
            return None, "used_skills entries must be non-empty strings"
    return used_skills, None


def _validate_verification(verification):
    if verification is None:
        return [], None
    if not isinstance(verification, list):
        return None, "verification must be a list"
    for entry in verification:
        if not isinstance(entry, dict):
            return None, "verification entries must be objects"
    return verification, None


def _validate_operator_question(question):
    if question is None:
        return None, None
    if not isinstance(question, dict):
        return None, "operator_question must be an object"
    if question.get("kind") not in {
        "clarification",
        "scope_decision",
        "completion_acceptance",
    }:
        return None, "operator_question.kind is invalid"
    summary = question.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
        return None, "operator_question.summary is invalid"
    choices = question.get("choices", [])
    if not isinstance(choices, list) or len(choices) > 5:
        return None, "operator_question.choices is invalid"
    for choice in choices:
        if not isinstance(choice, dict):
            return None, "operator_question choices must be objects"
        if not isinstance(choice.get("id"), str) or not choice["id"]:
            return None, "operator_question choice id is required"
        if not isinstance(choice.get("label"), str) or not choice["label"]:
            return None, "operator_question choice label is required"
    return question, None


def _optional_string_list(value):
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def build_result(
    task_id,
    attempt_id,
    output_type,
    consumed_event_fingerprints,
    verification,
    handoff,
    planned_github_actions=None,
    used_skills=None,
    memory_delta=None,
    used_memory_ids=None,
    recommended_route=None,
    branch_slug=None,
    review_point_evaluation=None,
    consumed_work_item_event_ids=None,
    operator_question=None,
):
    payload = {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "output_type": output_type,
        "planned_github_actions": list(planned_github_actions or []),
        "consumed_event_fingerprints": list(consumed_event_fingerprints),
        "consumed_work_item_event_ids": list(consumed_work_item_event_ids or []),
        "verification": list(verification or []),
        "handoff": handoff,
        "used_skills": list(used_skills or []),
    }
    if memory_delta is not None:
        payload["memory_delta"] = memory_delta
    if used_memory_ids is not None:
        payload["used_memory_ids"] = list(used_memory_ids)
    if recommended_route is not None:
        payload["recommended_route"] = recommended_route
    if branch_slug is not None:
        payload["branch_slug"] = branch_slug
    if review_point_evaluation is not None:
        if not isinstance(review_point_evaluation, list):
            raise ValueError("review_point_evaluation must be a list")
        payload["review_point_evaluation"] = list(review_point_evaluation)
    if operator_question is not None:
        payload["operator_question"] = operator_question
    return payload


def record_result(db_path, payload):
    if "planned_github_actions" not in payload:
        return {
            "ok": False,
            "status": "failed_validation",
            "safe_error": "planned_github_actions is required; actual_github_actions is legacy",
        }
    action_payload = payload.get("planned_github_actions")
    actions, action_error = _validate_actions(action_payload)
    if action_error:
        return {"ok": False, "status": "failed_validation", "safe_error": action_error}
    used_skills, skill_error = _validate_used_skills(payload.get("used_skills"))
    if skill_error:
        return {"ok": False, "status": "failed_validation", "safe_error": skill_error}
    verification, verification_error = _validate_verification(payload.get("verification", []))
    if verification_error:
        return {"ok": False, "status": "failed_validation", "safe_error": verification_error}
    local_event_ids = payload.get("consumed_work_item_event_ids", [])
    if not isinstance(local_event_ids, list) or not all(
        isinstance(event_id, str) and event_id for event_id in local_event_ids
    ):
        return {
            "ok": False,
            "status": "failed_validation",
            "safe_error": "consumed_work_item_event_ids must be a string list",
        }
    operator_question, question_error = _validate_operator_question(
        payload.get("operator_question")
    )
    if question_error:
        return {"ok": False, "status": "failed_validation", "safe_error": question_error}
    metadata = {
        "recorded_by": "robert.worker",
        "used_skills": used_skills,
        "memory_delta": payload.get(
            "memory_delta",
            {
                "status": "none",
                "reason": "worker did not provide reusable project memory",
            },
        ),
        "used_memory_ids": _optional_string_list(payload.get("used_memory_ids")),
        "review_point_evaluation": payload.get("review_point_evaluation", []),
        "consumed_work_item_event_ids": local_event_ids,
    }
    if operator_question is not None:
        metadata["operator_question"] = operator_question
    recommended_route = payload.get("recommended_route")
    if isinstance(recommended_route, str) and recommended_route:
        metadata["recommended_route"] = recommended_route
    branch_slug = payload.get("branch_slug")
    if isinstance(branch_slug, str) and branch_slug:
        metadata["branch_slug"] = branch_slug

    created_at = datetime.now(timezone.utc)
    result_created_at = created_at.isoformat()
    result_id = f"result-{uuid4().hex[:12]}"
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            """
            INSERT INTO worker_results(
              result_id, task_id, attempt_id, output_type,
              consumed_event_fingerprints_json, verification_json, handoff, created_at,
              metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result_id,
                payload["task_id"],
                payload["attempt_id"],
                payload["output_type"],
                json.dumps(payload["consumed_event_fingerprints"], sort_keys=True),
                json.dumps(verification, sort_keys=True),
                payload.get("handoff", ""),
                result_created_at,
                json.dumps(
                    metadata,
                    sort_keys=True,
                ),
            ),
        )
        for index, action in enumerate(actions):
            action_created_at = (created_at + timedelta(microseconds=index)).isoformat()
            conn.execute(
                """
                INSERT INTO github_actions(
                  action_id, result_id, task_id, action_type, target_url, external_id,
                  audit_status, publish_status, created_at, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, 'pending', 'not_published', ?, ?)
                """,
                (
                    f"action-{uuid4().hex[:12]}",
                    result_id,
                    payload["task_id"],
                    action["type"],
                    action.get("target_url") or action.get("url"),
                    action.get("id"),
                    action_created_at,
                    json.dumps(action, sort_keys=True),
                ),
            )
        cur = conn.execute(
            """
            UPDATE attempts
            SET status = 'completed', finished_at = ?
            WHERE attempt_id = ?
              AND status IN ('running', 'prepared', 'stale')
            """,
            (result_created_at, payload["attempt_id"]),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return {"ok": False, "status": "rejected_by_supervisor", "result_id": result_id}
        _insert_worker_result_wakeup(conn, payload, result_id, result_created_at)
    return {"ok": True, "status": "recorded", "result_id": result_id}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--output-type", required=True)
    parser.add_argument("--handoff", default="")
    args = parser.parse_args(argv)
    payload = json.load(sys.stdin)
    result = build_result(
        task_id=args.task_id,
        attempt_id=args.attempt_id,
        output_type=args.output_type,
        planned_github_actions=payload.get("planned_github_actions", []),
        consumed_event_fingerprints=payload.get("consumed_event_fingerprints", []),
        verification=payload.get("verification", []),
        handoff=args.handoff or payload.get("handoff", ""),
        used_skills=payload.get("used_skills", []),
        memory_delta=payload.get("memory_delta"),
        used_memory_ids=payload.get("used_memory_ids"),
        recommended_route=payload.get("recommended_route"),
        branch_slug=payload.get("branch_slug"),
        review_point_evaluation=payload.get("review_point_evaluation"),
        consumed_work_item_event_ids=payload.get("consumed_work_item_event_ids", []),
        operator_question=payload.get("operator_question"),
    )
    if args.db:
        output = record_result(args.db, result)
    else:
        output = {"ok": True, "status": "built", "result": result}
    return emit(output)


if __name__ == "__main__":
    raise SystemExit(main())
