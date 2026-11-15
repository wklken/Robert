"""Shared TAPD identity syntax and controller-only credential names."""

import re


DEFAULT_BOT_NAME = "robert-wklken"
CREDENTIAL_ENVIRONMENT = frozenset({"TAPD_ACCESS_TOKEN", "TAPD_API_USER", "TAPD_API_PASSWORD"})
_WORKSPACE_ID = re.compile(r"[0-9]{1,64}")
_COMMENT_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?")
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def workspace_id_is_valid(value: object) -> bool:
    return isinstance(value, str) and _WORKSPACE_ID.fullmatch(value) is not None


def canonical_host(value: str) -> str:
    labels = value.split(".")
    if len(value) > 253 or any(_DNS_LABEL.fullmatch(label) is None for label in labels):
        raise ValueError("TAPD host must be a DNS hostname without a URL path, port or credentials")
    return value.lower()


def comment_name(value: str, field: str) -> str:
    if not isinstance(value, str) or _COMMENT_NAME.fullmatch(value) is None:
        raise ValueError(f"{field} must be a visible TAPD identity without @, whitespace or Unicode variants")
    return value
