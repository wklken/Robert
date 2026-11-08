#!/usr/bin/env python3

from robert_agent.foundation.contracts.delivery import OutputType
from robert_agent.foundation.contracts.work import REMEDIATION_TASK_KINDS, TaskKind

import argparse
import json
import re
import sys

from robert_agent.foundation.commands import emit
from robert_agent.configuration.routes import RoutePolicy, load_route_policies


def _task_route(policy: RoutePolicy):
    route = dict(policy)
    route_id = route.pop("id")
    route["route_id"] = route_id
    route["confidence"] = "low" if route_id == "classification-result" else "high"
    route.setdefault("required_skills", [])
    route.setdefault("recommended_skills", [])
    return route


ROUTES = {policy["id"]: _task_route(policy) for policy in load_route_policies()}


def verification_policy_for(route_id):
    policy = ROUTES.get(route_id, {}).get("verification_policy") or {
        "mode": "optional",
        "required_statuses": ["passed"],
        "allow_skipped": True,
    }
    return dict(policy)


def review_input_policy(expected_output, trigger):
    """Select the review contract from controller-owned trigger metadata."""
    if (
        expected_output not in {OutputType.UPDATE_EXISTING_PR, OutputType.REVIEW_COMMENT}
        or trigger.get("task_kind") in REMEDIATION_TASK_KINDS
    ):
        return "none"
    if trigger.get("event_type") in {"comment", "manual_operator_request"}:
        return "classify"
    # Native reviews and unknown/legacy triggers retain the strict contract.
    return "required"


def route_task(task):
    task_kind = task.get("task_kind")
    if task_kind in REMEDIATION_TASK_KINDS:
        result = dict(ROUTES["update-existing-pr"])
        result["required_skills"] = (
            ["fast-test-fix", "fast-preflight"]
            if task_kind == TaskKind.CI_REMEDIATION
            else ["fast-conflict-fix", "fast-preflight"]
        )
        return result
    intent = task.get("intent")
    if intent == "vote" and task.get("origin_type") != "web":
        return dict(ROUTES["vote"])
    if task.get("origin_type") == "web" and intent in {
        "analysis",
        "discussion",
        "requirement_analysis",
    }:
        return dict(ROUTES["local-result"])
    if task.get("source_type") == "pull_request" and task.get("authorization_status") == "accepted_review_participant":
        return dict(ROUTES["review-comment"])
    if task.get("source_type") == "pull_request" and (
        task.get("event_type") == "review_request"
        or task.get("requested_reviewer")
        or task.get("requested_team")
        or (
            intent == "review_request"
            and task.get("authorization_status") == "authorized_trigger"
            and re.search(r"@\S+\s+(?:please\s+)?review\b", task.get("body") or "", re.I)
        )
    ):
        return dict(ROUTES["review-pr"])
    if task.get("source_type") == "pull_request" and task.get("has_open_dd_pr"):
        if intent == "waiting_for_user":
            return dict(ROUTES["waiting-for-user"])
        if intent in {
            "analysis",
            "discussion",
            "requirement_analysis",
            "review_request",
        }:
            return dict(ROUTES["review-comment"])
        if intent in {"bug_fix", "small_change", "add_tests", "pr_followup_fix"}:
            return dict(ROUTES["update-existing-pr"])
        return dict(ROUTES["classification-result"])
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
