"""TAPD identity syntax shared by configuration, the adapter and source owners."""

import re
from urllib.parse import urlparse

DEFAULT_MENTION_NAME = "robert-wklken"
ENVIRONMENT_ALLOWLIST = frozenset(
    {"HOME", "TAPD_ACCESS_TOKEN", "TAPD_API_USER", "TAPD_API_PASSWORD", "TAPD_API_BASE_URL", "TAPD_BASE_URL"}
)
SOURCE_TYPES = {"story": "tapd_story", "bug": "tapd_bug"}
KINDS = {source_type: kind for kind, source_type in SOURCE_TYPES.items()}
_URL_PATHS = {"story": ("prong", "stories"), "bug": ("bugtrace", "bugs")}
_NUMERIC_ID = re.compile(r"[1-9][0-9]{0,63}")
_COMMENT_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?")
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def numeric_id_is_valid(value: object) -> bool:
    """Reject 0 too: the CLI treats `--entry-id 0` as no filter."""
    return isinstance(value, str) and _NUMERIC_ID.fullmatch(value) is not None


def canonical_host(value: str) -> str:
    labels = value.split(".") if isinstance(value, str) else [""]
    if len(value) > 253 or any(_DNS_LABEL.fullmatch(label) is None for label in labels):
        raise ValueError("TAPD host must be a DNS hostname without a URL path, port or credentials")
    return value.lower()


def comment_name(value: str, field: str) -> str:
    if not isinstance(value, str) or _COMMENT_NAME.fullmatch(value) is None:
        raise ValueError(f"{field} must be a visible TAPD identity without @, whitespace or Unicode variants")
    return value


def project_key(host: str, workspace_id: str) -> str:
    return f"{host}/{workspace_id}"


def ticket_url(host: str, workspace_id: str, kind: str, external_id: str) -> str:
    area, collection = _URL_PATHS[kind]
    return f"https://{host}/{workspace_id}/{area}/{collection}/view/{external_id}"


def parse_ticket_url(url: object):
    """Return (host, workspace_id, kind, external_id) for a canonical ticket URL, else None."""
    if not isinstance(url, str):
        return None
    parsed = urlparse(url)
    parts = parsed.path.split("/")[1:]
    if parsed.scheme != "https" or parsed.query or parsed.fragment or parsed.port or len(parts) != 5:
        return None
    workspace_id, area, collection, view, external_id = parts
    kind = next((name for name, path in _URL_PATHS.items() if path == (area, collection)), None)
    if not kind or view != "view" or not numeric_id_is_valid(workspace_id) or not numeric_id_is_valid(external_id):
        return None
    try:
        host = canonical_host(parsed.hostname or "")
    except ValueError:
        return None
    return host, workspace_id, kind, external_id


def source_key(host: str, workspace_id: str, kind: str, external_id: str, repository: str) -> str:
    return f"tapd:{host}/{workspace_id}/{kind}/{external_id}:{repository}"


def is_source_key(value) -> bool:
    return isinstance(value, str) and value.startswith("tapd:")


def comment_fingerprint(host: str, workspace_id: str, comment_id: str) -> str:
    return f"tapd-comment:{host}/{workspace_id}/{comment_id}"
