"""Observe issue/PR state without scheduling or mutating local tasks."""
from robert_agent.adapters.github import client as github_client


def read_source_state(repo_full_name, source_type, number, runner):
    issue_payload = github_client.try_read_json(
        ["gh", "api", f"repos/{repo_full_name}/issues/{number}"],
        runner=runner,
    )
    if not isinstance(issue_payload, dict):
        return None
    remote = {
        "state": issue_payload.get("state") or "open",
        "state_reason": issue_payload.get("state_reason"),
        "title": issue_payload.get("title") or "",
        "html_url": issue_payload.get("html_url"),
        "updated_at": issue_payload.get("updated_at"),
        "closed_at": issue_payload.get("closed_at"),
        "merged": False,
        "merged_at": None,
    }
    if source_type != "pull_request" or remote["state"].lower() != "closed":
        return remote
    pull_request = issue_payload.get("pull_request")
    if isinstance(pull_request, dict) and "merged_at" in pull_request:
        remote["merged_at"] = pull_request["merged_at"]
        remote["merged"] = bool(pull_request["merged_at"])
        return remote
    pr_payload = github_client.try_read_json(
        ["gh", "api", f"repos/{repo_full_name}/pulls/{number}"],
        runner=runner,
    )
    if not isinstance(pr_payload, dict):
        return None
    remote["html_url"] = pr_payload.get("html_url") or remote["html_url"]
    remote["merged"] = bool(pr_payload.get("merged") or pr_payload.get("merged_at"))
    remote["merged_at"] = pr_payload.get("merged_at")
    return remote
