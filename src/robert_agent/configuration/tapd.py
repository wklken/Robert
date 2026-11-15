"""Normalize structurally validated TAPD v2 settings without external I/O."""

from pathlib import Path
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from robert_agent.foundation.tapd_identity import (
    DEFAULT_BOT_NAME,
    canonical_host,
    comment_name,
    workspace_id_is_valid,
)
from robert_agent.foundation.timestamps import utc_timestamp


def _exact(value, field):
    if (
        value != value.strip()
        or any(unicodedata.category(char).startswith("C") for char in value)
        or any(char.isspace() and char != " " for char in value)
    ):
        raise ValueError(f"{field} must be exact visible text without surrounding whitespace or controls")
    return value


def _host(value, field):
    try:
        return canonical_host(value)
    except ValueError as exc:
        raise ValueError(f"{field}: {exc}") from exc


def _workspace(value, field):
    if not workspace_id_is_valid(value):
        raise ValueError(f"{field} must be a digit string")
    return value


def _absolute(value, field):
    _exact(value, field)
    if not Path(value).is_absolute():
        raise ValueError(f"{field} must be an absolute path")
    return str(Path(value))


def _repair(rule, repository, field):
    policy = repository["pr_automation"]
    ci_config = policy["ci"]
    ci = {
        "enabled": rule["repair"]["ci"],
        "check_allowlist": list(ci_config["check_allowlist"]),
        "max_wall_minutes": ci_config["max_wall_minutes"],
        "max_total_tokens": ci_config["max_total_tokens"],
        "max_cost_usd": ci_config["max_cost_usd"],
    }
    if ci["enabled"] and (not ci["check_allowlist"] or not all(ci["check_allowlist"])):
        raise ValueError(f"{field}.ci.check_allowlist requires non-empty repository checks")
    return {
        "max_attempts": 3,
        "ci": ci,
        "conflict": {
            "enabled": rule["repair"]["conflict"],
            "max_wall_minutes": policy["conflict"]["max_wall_minutes"],
        },
    }


def _project(raw, repositories, field):
    workspace = _workspace(raw["workspace_id"], f"{field}.workspace_id")
    trusted_author = _exact(raw["trusted_author"], f"{field}.trusted_author")
    operators = [_exact(value, f"{field}.operators") for value in raw["operators"]]
    candidates = [_exact(value, f"{field}.repositories") for value in raw["repositories"]]
    if set(candidates) - repositories.keys():
        raise ValueError(f"{field}.repositories must reference configured repositories")

    mentions = dict(raw["mentions"])
    if "activated_after" in mentions or mentions["enabled"]:
        boundary = utc_timestamp(mentions.get("activated_after"))
        if boundary is None:
            raise ValueError(f"{field}.mentions.activated_after must be an ISO timestamp with an explicit timezone")
        mentions["activated_after"] = boundary.isoformat()

    rules = []
    seen = set()
    for index, rule in enumerate(raw["rules"]):
        path = f"{field}.rules[{index}]"
        for name in ("id", "assignee", "label", "repo"):
            _exact(rule[name], f"{path}.{name}")
        if rule["id"] in seen:
            raise ValueError(f"{path}.id duplicates a rule ID in this project")
        seen.add(rule["id"])
        if rule["repo"] not in candidates:
            raise ValueError(f"{path}.repo must be one of the project's repositories")
        rules.append({**rule, "repair": _repair(rule, repositories[rule["repo"]], f"{path}.repair")})

    return {
        **raw,
        "workspace_id": workspace,
        "trusted_author": trusted_author,
        "operators": operators,
        "repositories": candidates,
        "mentions": mentions,
        "rules": rules,
    }


def normalize(raw, *, repos, workers):
    result = dict(raw)
    if raw["enabled"] or raw["projects"]:
        for name in ("host", "executable", "cwd", "api_timezone"):
            if name not in raw:
                raise ValueError(f"tapd.{name} is required for configured projects")
    if "host" in raw:
        result["host"] = _host(raw["host"], "tapd.host")
    for name in ("executable", "cwd"):
        if name in raw:
            result[name] = _absolute(raw[name], f"tapd.{name}")
    if "api_timezone" in raw:
        name = _exact(raw["api_timezone"], "tapd.api_timezone")
        try:
            ZoneInfo(name)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("tapd.api_timezone must be an IANA timezone name") from exc
    if "model_worker" in raw and raw["model_worker"] not in workers:
        raise ValueError("tapd.model_worker must reference a configured worker")

    bot_name = comment_name(raw.get("bot_name", DEFAULT_BOT_NAME), "tapd.bot_name")
    result["bot_name"] = bot_name
    result["reply_name"] = comment_name(raw.get("reply_name", bot_name), "tapd.reply_name")
    repositories = {repo["full_name"]: repo for repo in repos}
    projects = [
        _project(project, repositories, f"tapd.projects[{index}]") for index, project in enumerate(raw["projects"])
    ]
    if len({project["workspace_id"] for project in projects}) != len(projects):
        raise ValueError("tapd.projects workspace_id entries must be unique")
    result["projects"] = projects

    if raw["enabled"]:
        raise ValueError(
            "tapd.enabled requires unavailable TAPD capability validation for author identity and ticket facts"
        )
    return result


def normalize_notification_projects(raw, tapd):
    available = {(tapd["host"], project["workspace_id"]) for project in tapd["projects"]}
    scopes = []
    seen = set()
    for index, scope in enumerate(raw):
        field = f"messaging.tapd_projects[{index}]"
        host = _host(scope["host"], f"{field}.host")
        workspace = _workspace(scope["workspace_id"], f"{field}.workspace_id")
        key = (host, workspace)
        if key not in available or key in seen:
            raise ValueError(f"{field} must reference a distinct configured TAPD project")
        seen.add(key)
        scopes.append({"host": host, "workspace_id": workspace})
    return scopes
