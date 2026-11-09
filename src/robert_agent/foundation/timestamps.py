"""Compare timezone-aware notification timestamps without losing microseconds."""

from datetime import datetime, timezone


def utc_timestamp(value):
    if not isinstance(value, str) or value != value.strip() or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def timestamp_after(value, boundary):
    occurred, start = utc_timestamp(value), utc_timestamp(boundary)
    return occurred is not None and start is not None and occurred > start
