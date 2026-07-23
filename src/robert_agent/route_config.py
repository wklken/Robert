import yaml

from robert_agent.resource_files import resource


ALLOWED_ROUTE_OVERRIDE_FIELDS = {
    "worker",
    "required_skills",
    "recommended_skills",
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
    return result
