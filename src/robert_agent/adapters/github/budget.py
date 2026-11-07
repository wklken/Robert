"""application github_budget responsibilities."""

from robert_agent.adapters.github import client as github_client
import time


def budget(session, config):
    payload = github_client.read_json(["gh", "api", "rate_limit"], runner=session)
    resources = payload.get("resources") or {}
    core = resources.get("core") or {}
    search = resources.get("search") or {}
    session.search_allowed = (
        search.get("remaining", 0) >= config["daemon"]["min_search_remaining"]
    )
    allowed = core.get("remaining", 0) >= config["daemon"]["min_core_remaining"]
    if not allowed:
        session.poll_interval_seconds = max(60, int(core.get("reset", 0) - time.time()))
    return allowed
