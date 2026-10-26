import yaml

from robert_agent.resource_files import resource


ALLOWED_ROUTE_OVERRIDE_FIELDS = {
    "worker",
    "required_skills",
    "recommended_skills",
    "multi_agent_review",
    "multi_agent_vote",
}


def load_route_policies():
    data = yaml.safe_load(
        resource("routes.yml").read_text(encoding="utf-8")
    )
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
        raise ValueError(
            f"{label} contains unsupported fields: {', '.join(unsupported)}"
        )
    return raw


def resolve_route_config(config, repo, route_policy):
    route_id = route_policy["id"]
    global_override = _validated_override(
        config.get("routes", {}).get(route_id),
        f"routes.{route_id}",
    )
    repo_override = _validated_override(
        repo.get("routes", {}).get(route_id),
        (
            "repos."
            f"{repo.get('full_name', 'unknown-repository')}."
            f"routes.{route_id}"
        ),
    )
    result = dict(route_policy)
    for field in ALLOWED_ROUTE_OVERRIDE_FIELDS:
        if field in global_override:
            result[field] = global_override[field]
        if field in repo_override:
            result[field] = repo_override[field]
    workers = config["workers"]
    default_worker = (
        next(iter(workers))
        if isinstance(workers, dict)
        else workers[0]["name"]
    )
    result.setdefault("worker", default_worker)
    result.setdefault("required_skills", [])
    result.setdefault("recommended_skills", [])
    if "multi_agent_review" in result:
        _validate_multi_review(result, workers)
    if "multi_agent_vote" in result:
        _validate_multi_vote(result, workers)
    return result


def _validate_multi_review(route, workers):
    settings = route["multi_agent_review"]
    if route["id"] != "review-pr":
        raise ValueError("multi_agent_review is only supported by review-pr")
    if not isinstance(settings, dict) or set(settings) - {
        "enabled", "workers", "author_allowlist"
    }:
        raise ValueError("multi_agent_review requires enabled, workers and author_allowlist")
    if not isinstance(settings.get("enabled", False), bool):
        raise ValueError("multi_agent_review.enabled must be a boolean")
    names = settings.get("workers", [route["worker"]])
    known = set(workers) if isinstance(workers, dict) else {w["name"] for w in workers}
    if (not isinstance(names, list) or not names
            or any(not isinstance(name, str) or name not in known for name in names)
            or len(set(names)) != len(names)):
        raise ValueError("multi_agent_review.workers must name distinct configured workers")
    if names[0] != route["worker"]:
        raise ValueError("the first review worker must equal routes.review-pr.worker")
    authors = settings.get("author_allowlist", [])
    if not isinstance(authors, list) or any(
        not isinstance(author, str) or not author.strip() for author in authors
    ):
        raise ValueError("multi_agent_review.author_allowlist must contain GitHub logins")
    route["multi_agent_review"] = {
        "enabled": settings.get("enabled", False), "workers": list(names),
        "author_allowlist": [author.strip().casefold() for author in authors],
    }


def review_workers_for_author(route, pr_author):
    """Routing permission is independent of trigger trust; only PR authors qualify."""
    settings = route.get("multi_agent_review") or {}
    if settings.get("enabled") and isinstance(pr_author, str) and (
        pr_author.casefold() in settings.get("author_allowlist", [])
    ):
        return list(settings["workers"])
    return [route["worker"]]


def _validate_multi_vote(route, workers):
    settings = route['multi_agent_vote']
    if route['id'] != 'vote' or not isinstance(settings, dict) or set(settings) - {'enabled', 'workers'}:
        raise ValueError('multi_agent_vote is only supported by vote and requires enabled/workers')
    if type(settings.get('enabled', False)) is not bool:
        raise ValueError('multi_agent_vote.enabled must be a boolean')
    names = settings.get('workers', [])
    known = set(workers) if isinstance(workers, dict) else {w['name'] for w in workers}
    if (not isinstance(names, list) or len(names) < 2
            or any(not isinstance(n, str) or n not in known for n in names)
            or len(set(names)) != len(names) or names[0] != route['worker']):
        raise ValueError('multi_agent_vote requires at least two distinct known workers with the main worker first')
    route['multi_agent_vote'] = {'enabled': settings.get('enabled', False), 'workers': list(names)}
