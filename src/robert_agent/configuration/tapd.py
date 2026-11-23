"""Normalize structurally validated TAPD settings without external I/O."""

import unicodedata

from robert_agent.foundation.tapd_identity import canonical_host, comment_name, numeric_id_is_valid
from robert_agent.foundation.timestamps import utc_timestamp


def _exact(value, field):
    if (
        not value.strip()
        or value != value.strip()
        or any(unicodedata.category(char).startswith("C") or (char.isspace() and char != " ") for char in value)
    ):
        raise ValueError(f"{field} must be exact visible text without surrounding whitespace or controls")
    return value


def _project(raw, repositories, field):
    if not numeric_id_is_valid(raw["workspace_id"]):
        raise ValueError(f"{field}.workspace_id must be a digit string")
    try:
        host = canonical_host(raw["host"])
    except ValueError as exc:
        raise ValueError(f"{field}.host: {exc}") from None
    start = utc_timestamp(raw["accept_after"])
    if start is None:
        raise ValueError(f"{field}.accept_after must be an ISO timestamp with an explicit timezone")
    rules = []
    for index, rule in enumerate(raw["label_rules"]):
        path = f"{field}.label_rules[{index}]"
        label = _exact(rule["label"], f"{path}.label")
        if any(existing["label"] == label for existing in rules):
            raise ValueError(f"{path}.label duplicates another label in this project")
        if rule["repository"] not in repositories:
            raise ValueError(f"{path}.repository must reference a configured repository")
        rules.append({"label": label, "repository": rule["repository"]})
    return {
        "host": host,
        "workspace_id": raw["workspace_id"],
        "trusted_user": _exact(raw["trusted_user"], f"{field}.trusted_user"),
        "accept_after": start.isoformat(),
        "label_rules": rules,
    }


def normalize(raw, *, repositories):
    projects = [
        _project(project, repositories, f"tapd.projects[{index}]") for index, project in enumerate(raw["projects"])
    ]
    if len({(project["host"], project["workspace_id"]) for project in projects}) != len(projects):
        raise ValueError("tapd.projects must not repeat a host and workspace_id")
    if raw["enabled"] and not projects:
        raise ValueError("tapd.enabled requires at least one project")
    return {
        "enabled": raw["enabled"],
        "executable": _exact(raw["executable"], "tapd.executable"),
        "mention_name": comment_name(raw["mention_name"], "tapd.mention_name"),
        "projects": projects,
    }
