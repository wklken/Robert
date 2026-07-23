#!/usr/bin/env python3
import argparse
import json
import sys

from robert_agent.common import emit


DEFAULT_VERIFICATION_POLICIES = {
    "comment-analysis": {
        "mode": "optional",
        "required_statuses": ["passed"],
        "allow_skipped": True,
    },
    "local-result": {
        "mode": "optional",
        "required_statuses": ["passed"],
        "allow_skipped": True,
    },
    "new-pr": {
        "mode": "required",
        "required_statuses": ["passed"],
        "allow_skipped": False,
    },
    "update-existing-pr": {
        "mode": "required_for_push",
        "required_statuses": ["passed"],
        "allow_skipped": False,
    },
    "review-comment": {
        "mode": "optional",
        "required_statuses": ["passed"],
        "allow_skipped": True,
    },
    "review-pr": {
        "mode": "optional",
        "required_statuses": ["passed"],
        "allow_skipped": True,
    },
    "classification-result": {
        "mode": "none",
        "required_statuses": [],
        "allow_skipped": False,
    },
    "waiting-for-user": {
        "mode": "none",
        "required_statuses": [],
        "allow_skipped": False,
    },
}


def verification_policy_for(route_id):
    route_config = ROUTES.get(route_id) or {}
    policy = route_config.get("verification_policy") or DEFAULT_VERIFICATION_POLICIES.get(
        route_id,
        {
            "mode": "optional",
            "required_statuses": ["passed"],
            "allow_skipped": True,
        },
    )
    return dict(policy)


ROUTES = {
    "local-result": {
        "route_id": "local-result",
        "expected_output": "local_result",
        "allowed_github_actions": [],
        "verification_policy": DEFAULT_VERIFICATION_POLICIES["local-result"],
        "recommended_skills": ["fast-code-path", "fast-zoom-out"],
        "needs_worktree": False,
        "workspace_mode": "none",
        "confidence": "high",
    },
    "comment-analysis": {
        "route_id": "comment-analysis",
        "expected_output": "comment_analysis",
        "allowed_github_actions": ["comment"],
        "verification_policy": DEFAULT_VERIFICATION_POLICIES["comment-analysis"],
        "recommended_skills": [
            "fast-code-path",
            "fast-zoom-out",
            "fast-verify-review-point",
        ],
        "needs_worktree": False,
        "workspace_mode": "analysis",
        "confidence": "high",
    },
    "new-pr": {
        "route_id": "new-pr",
        "expected_output": "new_pr",
        "allowed_github_actions": ["push_existing_pr", "open_pr", "comment"],
        "verification_policy": DEFAULT_VERIFICATION_POLICIES["new-pr"],
        "recommended_skills": [
            "fast-small-pr",
            "fast-code-path",
            "fast-add-tests",
            "fast-test-fix",
            "fast-preflight",
        ],
        "needs_worktree": True,
        "workspace_mode": "new_branch",
        "confidence": "high",
    },
    "update-existing-pr": {
        "route_id": "update-existing-pr",
        "expected_output": "update_existing_pr",
        "allowed_github_actions": ["push_existing_pr", "comment"],
        "verification_policy": DEFAULT_VERIFICATION_POLICIES["update-existing-pr"],
        "recommended_skills": [
            "fast-small-pr",
            "fast-verify-review-point",
            "fast-code-path",
            "fast-add-tests",
            "fast-test-fix",
            "fast-code-simplify",
            "fast-preflight",
        ],
        "needs_worktree": True,
        "workspace_mode": "existing_pr",
        "confidence": "high",
    },
    "review-comment": {
        "route_id": "review-comment",
        "expected_output": "review_comment",
        "allowed_github_actions": ["comment"],
        "verification_policy": DEFAULT_VERIFICATION_POLICIES["review-comment"],
        "recommended_skills": [
            "fast-review-github-pr",
            "fast-verify-review-point",
            "fast-code-path",
        ],
        "needs_worktree": False,
        "workspace_mode": "analysis",
        "confidence": "high",
    },
    "review-pr": {
        "route_id": "review-pr",
        "expected_output": "pr_review_comment",
        "allowed_github_actions": ["comment"],
        "verification_policy": DEFAULT_VERIFICATION_POLICIES["review-pr"],
        "recommended_skills": [
            "fast-review-github-pr",
            "fast-code-path",
        ],
        "needs_worktree": True,
        "worktree_mode": "review_pr",
        "workspace_mode": "review_pr",
        "confidence": "high",
    },
    "classification-result": {
        "route_id": "classification-result",
        "expected_output": "classification_result",
        "allowed_github_actions": [],
        "recommended_skills": [],
        "needs_worktree": False,
        "workspace_mode": "none",
        "verification_policy": DEFAULT_VERIFICATION_POLICIES["classification-result"],
        "confidence": "low",
    },
    "waiting-for-user": {
        "route_id": "waiting-for-user",
        "expected_output": "waiting_for_user",
        "allowed_github_actions": ["comment"],
        "recommended_skills": [],
        "needs_worktree": False,
        "workspace_mode": "none",
        "verification_policy": DEFAULT_VERIFICATION_POLICIES["waiting-for-user"],
        "confidence": "high",
    },
}

for route_config in ROUTES.values():
    route_config.setdefault("required_skills", [])
    route_config.setdefault("recommended_skills", [])


def route_task(task):
    intent = task.get("intent")
    if task.get("origin_type") == "web" and intent in {
        "analysis",
        "discussion",
        "requirement_analysis",
    }:
        return dict(ROUTES["local-result"])
    if (
        task.get("source_type") == "pull_request"
        and task.get("authorization_status") == "accepted_review_participant"
    ):
        return dict(ROUTES["review-comment"])
    if task.get("source_type") == "pull_request" and (
        task.get("event_type") == "review_request"
        or task.get("requested_reviewer")
        or task.get("requested_team")
    ):
        return dict(ROUTES["review-pr"])
    if task.get("source_type") == "pull_request" and task.get("has_open_dd_pr"):
        if intent == "waiting_for_user":
            return dict(ROUTES["waiting-for-user"])
        if intent in {"analysis", "discussion", "requirement_analysis", "review_request"}:
            return dict(ROUTES["review-comment"])
        return dict(ROUTES["update-existing-pr"])
    if (
        task.get("source_type") == "pull_request"
        and not task.get("has_open_dd_pr")
        and intent in {"bug_fix", "small_change", "add_tests", "pr_followup_fix"}
    ):
        return dict(ROUTES["review-comment"])
    if intent in {"analysis", "discussion", "requirement_analysis"}:
        return dict(ROUTES["comment-analysis"])
    if intent in {"bug_fix", "small_change", "add_tests"}:
        return dict(ROUTES["new-pr"])
    if intent == "pr_followup_fix" and task.get("has_open_dd_pr"):
        return dict(ROUTES["update-existing-pr"])
    if intent == "review_request":
        return dict(ROUTES["review-comment"])
    if intent == "waiting_for_user":
        return dict(ROUTES["waiting-for-user"])
    return dict(ROUTES["classification-result"])


def main(argv=None):
    _parser = argparse.ArgumentParser()
    _parser.parse_args(argv)
    task = json.load(sys.stdin)
    return emit({"ok": True, "status": "routed", "route": route_task(task)})


if __name__ == "__main__":
    raise SystemExit(main())
