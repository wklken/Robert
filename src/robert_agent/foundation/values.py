"""Small shared identifier, timestamp and JSON conversions."""

from datetime import datetime
from datetime import timezone
from uuid import uuid4
import json


def new_id(prefix):
    return f"{prefix}-{uuid4().hex[:12]}"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def parse_time(value):
    if isinstance(value, datetime):
        return value
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def repository_id(full_name):
    return f"repo:{full_name}"


def github_source_id(source_key):
    return f"source:{source_key}"


def github_event_id(fingerprint):
    return f"event:{fingerprint}"


def json_object(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def json_list(value):
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
