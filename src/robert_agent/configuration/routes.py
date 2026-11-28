"""Load packaged route policies and apply configured overrides."""

from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import TypedDict

import yaml

from robert_agent.resource_files import resource


ALLOWED_ROUTE_OVERRIDE_FIELDS = {
    "worker",
    "model",
    "effort",
    "required_skills",
    "recommended_skills",
    "multi_agent_review",
    "pre_pr_review",
    "multi_agent_vote",
}


class VerificationPolicy(TypedDict):
    mode: str
    required_statuses: list[str]
    allow_skipped: bool


class _RequiredRoutePolicy(TypedDict):
    id: str
    workspace_mode: str
    expected_output: str
    allowed_github_actions: list[str]
    needs_worktree: bool
    verification_policy: VerificationPolicy


class RoutePolicy(_RequiredRoutePolicy, total=False):
    worktree_mode: str
    recommended_skills: list[str]
    required_skills: list[str]


def load_route_policies() -> list[RoutePolicy]:
    # Read every time so editable installs and package replacements take effect.
    # Cache only parsing, by content, and keep caller mutations out of the cache.
    document = resource("routes.yml").read_text(encoding="utf-8")
    return deepcopy(_parse_route_policies(document))


@lru_cache(maxsize=1)
def _parse_route_policies(document: str) -> list[RoutePolicy]:
    data = yaml.safe_load(document)
    routes = data.get("routes") if isinstance(data, dict) else None
    if not isinstance(routes, list) or not routes:
        raise ValueError("packaged routes.yml must contain routes")
    return routes


def _validated_override(raw, label):
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be a mapping")
    unsupported = sorted(set(raw) - ALLOWED_ROUTE_OVERRIDE_FIELDS)
    if unsupported:
        raise ValueError(f"{label} contains unsupported fields: {', '.join(unsupported)}")
    return raw


def resolve_route_config(config, repo, route_policy):
    route_id = route_policy["id"]
    global_override = _validated_override(
        config.get("routes", {}).get(route_id),
        f"routes.{route_id}",
    )
    repo_override = _validated_override(
        repo.get("routes", {}).get(route_id),
        (f"repos.{repo.get('full_name', 'unknown-repository')}.routes.{route_id}"),
    )
    if "pre_pr_review" in global_override or ("pre_pr_review" in repo_override and route_id != "new-pr"):
        raise ValueError("pre_pr_review is only supported by repository routes.new-pr")
    result = dict(route_policy)
    for field in ALLOWED_ROUTE_OVERRIDE_FIELDS:
        if field in global_override:
            result[field] = global_override[field]
        if field in repo_override:
            result[field] = repo_override[field]
    workers = config["workers"]
    default_worker = config["default_worker"]
    default_name = default_worker["name"] if isinstance(default_worker, dict) else default_worker
    result.setdefault("worker", default_name)
    by_name = workers if isinstance(workers, dict) else {w["name"]: w for w in workers}
    if result["worker"] not in by_name:
        raise ValueError(f"route {route_id} references unknown worker: {result['worker']}")
    selected = by_name[result["worker"]]
    result.setdefault("model", selected.get("default_model", selected.get("model", "default")))
    result.setdefault("effort", selected.get("default_effort", selected.get("effort", "default")))
    result.setdefault("required_skills", [])
    result.setdefault("recommended_skills", [])
    if "pre_pr_review" in result:
        _validate_pre_pr_review(result, by_name)
    if "multi_agent_review" in result:
        _validate_multi_review(result, workers)
    if "multi_agent_vote" in result:
        _validate_multi_vote(result, workers)
    return result


def _validate_pre_pr_review(route, workers):
    review = route["pre_pr_review"]
    if not isinstance(review, dict) or set(review) - {"workers", "deployment_validation"}:
        raise ValueError("pre_pr_review supports workers and deployment_validation")
    names = review.get("workers")
    if (
        not isinstance(names, list)
        or not names
        or any(not isinstance(name, str) or name not in workers for name in names)
        or len(set(names)) != len(names)
    ):
        raise ValueError("pre_pr_review.workers must name distinct configured workers")
    normalized = {"workers": list(names)}
    if "deployment_validation" in review:
        validation = review["deployment_validation"]
        if not isinstance(validation, dict) or set(validation) != {"worker", "skill_path"}:
            raise ValueError("pre_pr_review.deployment_validation requires worker and skill_path")
        name, skill_path = validation["worker"], validation["skill_path"]
        if not isinstance(name, str) or name not in workers:
            raise ValueError("pre_pr_review.deployment_validation.worker must name a configured worker")
        if (
            not isinstance(skill_path, str)
            or not skill_path.isprintable()
            or skill_path != skill_path.strip()
            or not Path(skill_path).is_absolute()
            or Path(skill_path).name != "SKILL.md"
        ):
            raise ValueError("pre_pr_review.deployment_validation.skill_path must be an absolute SKILL.md path")
        normalized["deployment_validation"] = {"worker": name, "skill_path": skill_path}
    route["pre_pr_review"] = normalized


def _validate_multi_review(route, workers):
    settings = route["multi_agent_review"]
    if route["id"] != "review-pr":
        raise ValueError("multi_agent_review is only supported by review-pr")
    if not isinstance(settings, dict) or set(settings) - {
        "enabled",
        "workers",
        "author_allowlist",
    }:
        raise ValueError("multi_agent_review requires enabled, workers and author_allowlist")
    if not isinstance(settings.get("enabled", False), bool):
        raise ValueError("multi_agent_review.enabled must be a boolean")
    names = settings.get("workers", [route["worker"]])
    known = set(workers) if isinstance(workers, dict) else {w["name"] for w in workers}
    if (
        not isinstance(names, list)
        or not names
        or any(not isinstance(name, str) or name not in known for name in names)
        or len(set(names)) != len(names)
    ):
        raise ValueError("multi_agent_review.workers must name distinct configured workers")
    if names[0] != route["worker"]:
        raise ValueError("the first review worker must equal routes.review-pr.worker")
    authors = settings.get("author_allowlist", [])
    if not isinstance(authors, list) or any(not isinstance(author, str) or not author.strip() for author in authors):
        raise ValueError("multi_agent_review.author_allowlist must contain GitHub logins")
    route["multi_agent_review"] = {
        "enabled": settings.get("enabled", False),
        "workers": list(names),
        "author_allowlist": [author.strip().casefold() for author in authors],
    }


def review_workers_for_author(route, pr_author):
    """Routing permission is independent of trigger trust; only PR authors qualify."""
    settings = route.get("multi_agent_review") or {}
    if (
        settings.get("enabled")
        and isinstance(pr_author, str)
        and (pr_author.casefold() in settings.get("author_allowlist", []))
    ):
        return list(settings["workers"])
    return [route["worker"]]


def _validate_multi_vote(route, workers):
    settings = route["multi_agent_vote"]
    if route["id"] != "vote" or not isinstance(settings, dict) or set(settings) - {"enabled", "workers"}:
        raise ValueError("multi_agent_vote is only supported by vote and requires enabled/workers")
    if type(settings.get("enabled", False)) is not bool:
        raise ValueError("multi_agent_vote.enabled must be a boolean")
    names = settings.get("workers", [])
    known = set(workers) if isinstance(workers, dict) else {w["name"] for w in workers}
    if (
        not isinstance(names, list)
        or len(names) < 2
        or any(not isinstance(n, str) or n not in known for n in names)
        or len(set(names)) != len(names)
        or names[0] != route["worker"]
    ):
        raise ValueError("multi_agent_vote requires at least two distinct known workers with the main worker first")
    route["multi_agent_vote"] = {
        "enabled": settings.get("enabled", False),
        "workers": list(names),
    }


ROUTE_POLICIES = {policy["id"]: policy for policy in load_route_policies()}
