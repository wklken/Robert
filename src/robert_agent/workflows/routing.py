"""Resolve workflow policy, current authority and installed skill requirements."""

from robert_agent.foundation.contracts.delivery import GitHubActionType, OutputType

from robert_agent.adapters.github import discovery as discover
from robert_agent.workflows import routes as route
from robert_agent.context import skills
from robert_agent.configuration import routes as route_config
from robert_agent.work import source_policy


def effective_route(config_result, repo, event, route_id=None):
    if (
        config_result is not None
        and not route_id
        and event.get("source_type") == "pull_request"
        and event.get("authorization_status") == "accepted_context"
        and (event.get("actor_login") or "").casefold()
        in {login.casefold() for login in repo.get("trusted_actors", [])}
        and event.get("intent") == "review_request"
    ):
        candidate = route_config.resolve_route_config(config_result, repo, route_config.ROUTE_POLICIES["review-pr"])
        if len(route_config.review_workers_for_author(candidate, event.get("pr_author_login"))) > 1:
            event = {**event, "authorization_status": "authorized_trigger"}
    base_route = dict(route.ROUTES[route_id]) if route_id else route.route_task(event)
    if config_result is None:
        return base_route
    if (
        not route_id
        and event.get("source_type") == "pull_request"
        and event.get("event_type") == "assigned"
        and event.get("authorization_status") == "authorized_trigger"
        and event.get("intent") in {None, "unclear", "review_request"}
    ):
        candidate = route_config.resolve_route_config(config_result, repo, route_config.ROUTE_POLICIES["review-pr"])
        if len(route_config.review_workers_for_author(candidate, event.get("pr_author_login"))) > 1:
            base_route = dict(route.ROUTES["review-pr"])
    policy = route_config.ROUTE_POLICIES[base_route["route_id"]]
    resolved = route_config.resolve_route_config(
        config_result,
        repo,
        policy,
    )
    if resolved["id"] == "review-pr":
        selected = route_config.review_workers_for_author(resolved, event.get("pr_author_login"))
        if len(selected) > 1:
            configured = {worker["name"]: worker for worker in config_result["workers"]}
            resolved.update(
                native_review_workers=[configured[name] for name in selected],
                expected_output=OutputType.MULTI_AGENT_REVIEW_REPORT.value,
                allowed_github_actions=[GitHubActionType.SUBMIT_PR_REVIEW.value],
                required_skills=[],
                recommended_skills=[],
            )
    if resolved["id"] == "vote":
        settings = resolved.get("multi_agent_vote") or {}
        if (
            not settings.get("enabled")
            or not event.get("mentions_dd")
            or not discover.bot_requests(event.get("body"), repo["github_account"])
            or (event.get("actor_login") or "").casefold()
            not in {login.casefold() for login in repo.get("trusted_actors", [])}
            or not (route_id == "vote" or discover.is_vote_request(event.get("body"), repo["github_account"]))
        ):
            # Preserve the request as a visible explanation, never substitute a single-model vote.
            resolved.update(
                id="waiting-for-user",
                expected_output=OutputType.WAITING_FOR_USER.value,
                allowed_github_actions=["comment"],
                required_skills=[],
                recommended_skills=[],
                routing_block_reason="Multi-Agent voting requires routes.vote.multi_agent_vote.enabled, at least two configured workers, and an explicit trusted @ request. Explain this missing configuration/authority; do not perform a single-model substitute vote.",
            )
        else:
            configured = {worker["name"]: worker for worker in config_result["workers"]}
            resolved.update(
                native_vote_workers=[configured[name] for name in settings["workers"]],
                required_skills=[],
                recommended_skills=[],
            )
    installed = skills.discover_skill_names(config_result["skills"]["search_paths"])
    required_skills = list(resolved["required_skills"])
    for skill_name in base_route.get("required_skills", []):
        if skill_name not in required_skills:
            required_skills.append(skill_name)
    skill_status = skills.route_skill_status(
        required=required_skills,
        recommended=resolved["recommended_skills"],
        installed=installed,
    )
    return {
        **resolved,
        "route_id": resolved["id"],
        "confidence": base_route.get("confidence", "low"),
        "needs_worktree": base_route.get("needs_worktree", False),
        "enforce_workspace_policy": True,
        "required_skills": required_skills,
        "skill_status": skill_status,
    }


def native_request_allowed(entry, repo, config_result):
    authorized = source_policy.reauthorize_saved_request(entry["request"]["event"], repo)
    route_id = "vote" if "source_type" in entry else "review-pr"
    current_route = effective_route(config_result, repo, authorized, route_id=route_id)
    workers_key = "native_vote_workers" if route_id == "vote" else "native_review_workers"
    return bool(authorized.get("drives_execution") and current_route.get(workers_key))
