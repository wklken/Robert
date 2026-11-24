"""Conservative equivalence of independently authorized, full PR reviews."""

import hashlib
import json
import re

from robert_agent.work import source_policy


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def revision(pr):
    if not isinstance(pr, dict) or pr.get("state") != "open":
        return None
    head, base = pr.get("head") or {}, pr.get("base") or {}
    if not all(
        re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value or "") for value in (head.get("sha"), base.get("sha"))
    ):
        return None
    if not isinstance(base.get("ref"), str) or not base["ref"]:
        return None
    return {"head_sha": head["sha"], "base_sha": base["sha"], "base_ref": base["ref"]}


def fixture_pr(event):
    """An explicit fixture revision is evidence; absent fields remain unknown."""
    return {
        "number": event["number"],
        "state": event.get("state"),
        "head": {"sha": event.get("head_sha") or event.get("existing_pr_head_sha")},
        "base": {"sha": event.get("base_sha"), "ref": event.get("base_ref") or event.get("base_branch")},
        "user": {"login": event.get("pr_author_login")},
        "body": event.get("source_body", ""),
        "title": event.get("title", ""),
    }


def canonical_request(repo, event, route, workers, pr):
    """None means independent execution; prose never grants review authority."""
    if event.get("source_type") != "pull_request" or event.get("provider", "github") != "github":
        return None
    authorized = source_policy.reauthorize_saved_request(event, repo)
    if not authorized.get("drives_execution") or authorized.get("authorization_status") not in {
        "authorized_trigger",
        "accepted_context",
    }:
        return None
    actor = event.get("requester_login") if event.get("event_type") == "review_request" else event.get("actor_login")
    trusted = sorted({name.casefold() for name in repo.get("trusted_actors", [])})
    if (actor or event.get("actor_login") or "").casefold() not in trusted:
        return None
    if event.get("authorization_lookup_complete") is False or event.get("body_authorization_complete") is False:
        return None
    pinned = revision(pr)
    if not pinned or pr.get("number") != event["number"]:
        return None
    author = (pr.get("user") or {}).get("login", "")
    if not author or author.casefold() != (event.get("pr_author_login") or "").casefold():
        return None
    if not route.get("native_review_workers") or len(workers) < 2:
        return None
    # Only a deliberately narrow generic command can be equivalent to GitHub's
    # full-PR review request. Additional prose, even in quoted/code text, stays
    # independent rather than silently dropping requirements or evidence.
    body = event.get("body") or ""
    kind = event.get("event_type")
    if kind == "comment":
        command = rf"@{re.escape(repo['github_account'])}\s+(?:please\s+)?review(?:\s+(?:this|the)\s+(?:PR|pull request))?[.!]?"
        if not re.fullmatch(command, body.strip(), re.I) or event.get("intent") != "review_request":
            return None
    elif kind == "review_request":
        if event.get("intent") not in {None, "unclear", "review_request"} or (body and body != pr.get("body", "")):
            return None
    else:
        return None
    extra_fields = (
        "focus",
        "review_focus",
        "additional_focus",
        "review_scope",
        "materials",
        "additional_materials",
        "attachments",
        "clarifications",
        "independent_review",
        "independent_rereview",
        "force_review",
        "rereview",
    )
    if any(event.get(field) or (event.get("metadata") or {}).get(field) for field in extra_fields):
        return None
    acceptance = {
        "intent": "full_pr_review",
        "title": pr.get("title", ""),
        "source_body": pr.get("body", ""),
        "acceptance_intent": event.get("acceptance_intent"),
        "acceptance_criteria": event.get("acceptance_criteria"),
        "metadata": event.get("metadata") or {},
    }
    identity = {
        "schema_version": 1,
        "repo": repo["full_name"].casefold(),
        "pr_number": event["number"],
        **pinned,
        "scope": {"source_type": "pull_request", "number": event["number"], "paths": "all"},
        "workers": [dict(worker) for worker in workers],
        "route": {key: value for key, value in route.items() if key not in {"confidence", "branch_slug"}},
        "acceptance": acceptance,
        "eligibility": {
            "pr_author": author.casefold(),
            "github_account": repo["github_account"].casefold(),
            "trusted_actors": trusted,
        },
    }
    identity = json.loads(encoded(identity))
    return {"key": "review-execution:" + hashlib.sha256(encoded(identity).encode()).hexdigest(), "identity": identity}
