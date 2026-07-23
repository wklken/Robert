#!/usr/bin/env python3
import argparse
import json
import re
import sys

from robert_agent.common import emit
from robert_agent import redaction


PUBLIC_TEXT_FIELDS = {
    "body",
    "title",
    "summary",
    "text",
    "comment",
    "pr_body",
    "review_body",
}

EXPECTED_ACTIONS_FOR_OUTPUT = {
    "local_result": set(),
    "comment_analysis": {"comment"},
    "new_pr": {"open_pr", "push_existing_pr"},
    "update_existing_pr": {"push_existing_pr", "comment"},
    "review_comment": {"comment"},
    "pr_review_comment": {"comment"},
    "waiting_for_user": {"comment"},
}
VALID_OPERATOR_QUESTION_KINDS = {
    "clarification",
    "scope_decision",
    "completion_acceptance",
}
REVIEW_EVALUATION_OUTPUTS = {"update_existing_pr", "review_comment"}
VALID_REVIEW_VERDICTS = {"correct", "partially_correct", "incorrect", "unverified"}
VALID_REVIEW_ACTIONS = {"implement", "skip", "comment", "clarify"}
IMPLEMENTABLE_VERDICTS = {"correct", "partially_correct"}
VALID_VERIFICATION_STATUSES = {"passed", "failed", "skipped"}
CLASSIFICATION_RECOMMENDED_ROUTES = {
    "comment-analysis",
    "new-pr",
    "update-existing-pr",
    "review-comment",
    "review-pr",
    "waiting-for-user",
}
BRANCH_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_BRANCH_SLUG_LENGTH = 60

COMMENT_MARKER_RE = re.compile(
    r"<!--\s*(?:robert-comment|dd-comment)\b.*?-->",
    re.I | re.S,
)
WORKSTREAM_MARKER_RE = re.compile(
    r"<!--\s*(?:robert-workstream|dd-workstream)\b.*?-->",
    re.I | re.S,
)


def _validate_actions(actions):
    actual_types = []
    for action in actions:
        if not isinstance(action, dict):
            return None, "planned_github_actions entries must be objects"
        action_type = action.get("type")
        if not isinstance(action_type, str) or not action_type:
            return None, "planned_github_actions[*].type must be a non-empty string"
        actual_types.append(action_type)
    return actual_types, None


def _validate_used_skills(used_skills):
    if not isinstance(used_skills, list):
        return None, "used_skills must be a list"
    for skill in used_skills:
        if not isinstance(skill, str) or not skill:
            return None, "used_skills entries must be non-empty strings"
    return used_skills, None


def _audit_required_skills_coverage(used_skills, required_skills):
    if not required_skills:
        return None
    used = set(used_skills or [])
    missing = [skill for skill in required_skills if skill not in used]
    if missing:
        return f"used_skills missing required skills: {sorted(missing)}"
    return None


def _string_value(action, *keys):
    for key in keys:
        value = action.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _missing_fields(action, fields):
    return [field for field in fields if not _string_value(action, field)]


def _audit_publisher_requirements(actions):
    for action in actions:
        action_type = action.get("type")
        if action_type == "comment":
            missing = []
            if not _string_value(action, "target_url", "url"):
                missing.append("target_url")
            body = _string_value(action, "body")
            if not body:
                missing.append("body")
            if missing:
                return f"comment action missing fields: {missing}"
            if not COMMENT_MARKER_RE.search(body):
                return "comment action body requires robert-comment idempotency marker"
            continue
        if action_type == "open_pr":
            missing = _missing_fields(action, ["repo", "head", "base", "title", "body"])
            if missing:
                return f"open_pr action missing fields: {missing}"
            if not WORKSTREAM_MARKER_RE.search(action["body"]):
                return "open_pr action body requires robert-workstream metadata"
            continue
        if action_type == "push_existing_pr":
            missing = _missing_fields(action, ["worktree_path", "branch"])
            if missing:
                return f"push_existing_pr action missing fields: {missing}"
    return None


def _audit_expected_actions(output_type, actual_types):
    expected_types = EXPECTED_ACTIONS_FOR_OUTPUT.get(output_type, set())
    if not expected_types:
        return None
    if output_type == "update_existing_pr":
        if "comment" not in actual_types:
            return (
                f"output_type {output_type} requires planned_github_actions "
                "to include comment"
            )
        if "push_existing_pr" in actual_types:
            last_push = max(
                index for index, action_type in enumerate(actual_types) if action_type == "push_existing_pr"
            )
            if not any(
                index > last_push and action_type == "comment"
                for index, action_type in enumerate(actual_types)
            ):
                return f"output_type {output_type} requires comment after push_existing_pr"
        return None
    missing = sorted(expected_types.difference(actual_types))
    if missing:
        return (
            f"output_type {output_type} requires planned_github_actions to include "
            f"{missing}"
        )
    if output_type == "new_pr" and actual_types.index("push_existing_pr") > actual_types.index("open_pr"):
        return "output_type new_pr requires push_existing_pr before open_pr"
    return None


def _audit_pr_review_author_mention(result):
    if result.get("output_type") != "pr_review_comment":
        return None
    comment_actions = [
        action
        for action in result.get("planned_github_actions", [])
        if isinstance(action, dict) and action.get("type") == "comment"
    ]
    for action in comment_actions:
        pr_author_login = _string_value(action, "pr_author_login")
        if not pr_author_login:
            return "pr_review_comment comment action requires pr_author_login"
        required_mention = f"@{pr_author_login}"
        if required_mention not in _string_value(action, "body"):
            return f"pr_review_comment comment body must mention {required_mention}"
    return None


def _audit_review_point_evaluation(result, actual_types):
    output_type = result.get("output_type")
    if output_type not in REVIEW_EVALUATION_OUTPUTS:
        return None
    evaluation = result.get("review_point_evaluation")
    if not isinstance(evaluation, list) or not evaluation:
        return ["missing_review_point_evaluation"]
    for entry in evaluation:
        if not isinstance(entry, dict):
            return ["invalid_review_point_evaluation"]
        for field in ("summary", "verdict", "reasoning", "action"):
            if not isinstance(entry.get(field), str) or not entry.get(field):
                return ["invalid_review_point_evaluation"]
        if entry["verdict"] not in VALID_REVIEW_VERDICTS:
            return ["invalid_review_point_evaluation"]
        if entry["action"] not in VALID_REVIEW_ACTIONS:
            return ["invalid_review_point_evaluation"]
        if entry["action"] == "implement" and entry["verdict"] not in IMPLEMENTABLE_VERDICTS:
            return ["invalid_review_point_evaluation"]
    if output_type == "update_existing_pr" and "push_existing_pr" in actual_types:
        if not any(entry.get("action") == "implement" for entry in evaluation):
            return ["no_implemented_review_points"]
    return None


def _audit_classification_recommendation(result):
    if result.get("output_type") != "classification_result":
        return None
    recommended_route = result.get("recommended_route")
    if recommended_route in (None, ""):
        return None
    if not isinstance(recommended_route, str):
        return "classification_result recommended_route must be a string"
    if recommended_route not in CLASSIFICATION_RECOMMENDED_ROUTES:
        return f"classification_result recommended_route is not supported: {recommended_route}"
    branch_slug = result.get("branch_slug")
    if branch_slug in (None, ""):
        return None
    if not isinstance(branch_slug, str):
        return "classification_result branch_slug must be a string"
    if len(branch_slug) > MAX_BRANCH_SLUG_LENGTH or not BRANCH_SLUG_RE.fullmatch(branch_slug):
        return (
            "classification_result branch_slug must be lowercase ASCII kebab-case "
            f"and at most {MAX_BRANCH_SLUG_LENGTH} characters"
        )
    return None


def _verification_policy_error(message):
    return f"verification policy failed: {message}"


def _verification_required(verification_policy, actual_types):
    mode = (verification_policy or {}).get("mode", "optional")
    if mode == "required":
        return True
    if mode == "required_for_push":
        return "push_existing_pr" in actual_types
    return False


def _audit_verification_entry(entry, allow_skipped):
    if not isinstance(entry, dict):
        return "verification entries must be objects"
    command = entry.get("command")
    if not command:
        return "verification entry command is required"
    if isinstance(command, list):
        if not command or not all(isinstance(item, str) and item for item in command):
            return "verification entry command list must contain non-empty strings"
    elif not isinstance(command, str):
        return "verification entry command must be a string or string list"
    status = entry.get("status")
    if status not in VALID_VERIFICATION_STATUSES:
        return f"verification entry status must be one of {sorted(VALID_VERIFICATION_STATUSES)}"
    purpose = entry.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        return "verification entry purpose is required"
    if status == "skipped" and not entry.get("skipped_reason"):
        return "skipped verification entry requires skipped_reason"
    if entry.get("required") is True and status == "skipped" and not allow_skipped:
        return "required verification cannot be skipped for this route"
    if "exit_code" in entry and not isinstance(entry.get("exit_code"), int):
        return "verification entry exit_code must be an integer"
    if "required" in entry and not isinstance(entry.get("required"), bool):
        return "verification entry required must be a boolean"
    return None


def _audit_verification_contract(result, verification_policy, actual_types):
    if verification_policy is None:
        return None
    mode = verification_policy.get("mode", "optional")
    if mode == "none":
        return None
    verification = result.get("verification", [])
    if not isinstance(verification, list):
        return _verification_policy_error("verification must be a list")
    allow_skipped = bool(verification_policy.get("allow_skipped", False))
    for entry in verification:
        entry_error = _audit_verification_entry(entry, allow_skipped)
        if entry_error:
            return _verification_policy_error(entry_error)
    if not _verification_required(verification_policy, actual_types):
        return None
    required_statuses = set(verification_policy.get("required_statuses") or ["passed"])
    has_required_pass = any(
        entry.get("required") is True and entry.get("status") in required_statuses
        for entry in verification
    )
    if not has_required_pass:
        return _verification_policy_error(
            f"route requires a required verification entry with status in {sorted(required_statuses)}"
        )
    return None


def _audit_public_text(actions):
    needs_redaction = False
    for action in actions:
        for field in PUBLIC_TEXT_FIELDS:
            value = action.get(field)
            if not isinstance(value, str) or not value:
                continue
            result = redaction.redact_text(value)
            if not result["ok"]:
                return "redaction_blocked"
            if result.get("text") != value:
                needs_redaction = True
    if needs_redaction:
        return "redaction_required"
    return None


def _audit_operator_question(result):
    question = result.get("operator_question")
    if not isinstance(question, dict):
        return "waiting_for_user requires operator_question"
    if question.get("kind") not in VALID_OPERATOR_QUESTION_KINDS:
        return "operator_question.kind is invalid"
    summary = question.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
        return "operator_question.summary is invalid"
    choices = question.get("choices", [])
    if not isinstance(choices, list) or len(choices) > 5:
        return "operator_question.choices is invalid"
    for choice in choices:
        if not isinstance(choice, dict):
            return "operator_question choices must be objects"
        if not isinstance(choice.get("id"), str) or not choice["id"]:
            return "operator_question choice id is required"
        if not isinstance(choice.get("label"), str) or not choice["label"]:
            return "operator_question choice label is required"
    return None


def audit_result(
    result,
    allowed_github_actions,
    recommended_skills=None,
    required_skills=None,
    expected_output=None,
    verification_policy=None,
    origin_type="github",
):
    if origin_type == "web":
        if not result.get("consumed_work_item_event_ids"):
            return {
                "ok": False,
                "status": "failed",
                "safe_error": "consumed_work_item_event_ids must list handled events",
            }
    elif not result.get("consumed_event_fingerprints"):
        return {
            "ok": False,
            "status": "failed",
            "safe_error": "consumed_event_fingerprints must list handled events",
        }
    skill_guidance = recommended_skills if recommended_skills is not None else required_skills
    if skill_guidance is not None:
        used_skills, skill_error = _validate_used_skills(result.get("used_skills"))
        if skill_error:
            return {
                "ok": False,
                "status": "failed",
                "safe_error": skill_error,
            }
        coverage_error = _audit_required_skills_coverage(used_skills, required_skills)
        if coverage_error:
            return {
                "ok": False,
                "status": "failed",
                "safe_error": coverage_error,
            }

    allowed = set(allowed_github_actions)
    actions = result.get("planned_github_actions", [])
    actual_types, action_error = _validate_actions(actions)
    if action_error:
        return {
            "ok": False,
            "status": "failed",
            "safe_error": action_error,
        }

    violations = sorted({action_type for action_type in actual_types if action_type not in allowed})
    if violations:
        return {
            "ok": False,
            "status": "policy_violation",
            "violations": violations,
        }
    if origin_type == "web" and "comment" in actual_types:
        return {
            "ok": False,
            "status": "policy_violation",
            "violations": ["web_origin_github_action"],
        }

    output_type = result.get("output_type")
    waiting_alternate = output_type == "waiting_for_user"
    if expected_output and output_type != expected_output and not waiting_alternate:
        return {
            "ok": False,
            "status": "failed",
            "safe_error": f"expected_output {expected_output} does not match result output_type {output_type}",
        }
    if waiting_alternate:
        if origin_type == "web" or result.get("operator_question") is not None:
            question_error = _audit_operator_question(result)
            if question_error:
                return {
                    "ok": False,
                    "status": "failed",
                    "safe_error": question_error,
                }
        if origin_type == "github" and "comment" not in actual_types:
            return {
                "ok": False,
                "status": "failed",
                "safe_error": "GitHub waiting_for_user requires a public comment action",
            }

    redaction_violation = _audit_public_text(actions)
    if redaction_violation:
        return {
            "ok": False,
            "status": "policy_violation",
            "violations": [redaction_violation],
        }

    publisher_error = _audit_publisher_requirements(actions)
    if publisher_error:
        return {
            "ok": False,
            "status": "failed",
            "safe_error": publisher_error,
        }

    expected_action_error = (
        None
        if origin_type == "web" and output_type == "waiting_for_user"
        else _audit_expected_actions(output_type, actual_types)
    )
    if expected_action_error:
        return {
            "ok": False,
            "status": "failed",
            "safe_error": expected_action_error,
        }

    pr_review_author_error = _audit_pr_review_author_mention(result)
    if pr_review_author_error:
        return {
            "ok": False,
            "status": "failed",
            "safe_error": pr_review_author_error,
        }

    verification_error = _audit_verification_contract(result, verification_policy, actual_types)
    if verification_error:
        return {
            "ok": False,
            "status": "failed",
            "safe_error": verification_error,
        }

    review_violation = _audit_review_point_evaluation(result, actual_types)
    if review_violation:
        return {
            "ok": False,
            "status": "policy_violation",
            "violations": review_violation,
        }

    classification_error = _audit_classification_recommendation(result)
    if classification_error:
        return {
            "ok": False,
            "status": "failed",
            "safe_error": classification_error,
        }

    return {
        "ok": True,
        "status": "accepted",
        "planned_github_actions": actual_types,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--allowed-action", action="append", default=[])
    args = parser.parse_args(argv)
    result = json.load(sys.stdin)
    return emit(audit_result(result, args.allowed_action))


if __name__ == "__main__":
    raise SystemExit(main())
