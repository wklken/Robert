"""Pure limits for worker-proposed project memory, before persistence or prompts."""

import json

from robert_agent.security.redaction import redact_text

MAX_MEMORY_BYTES = 64 * 1024
MAX_MEMORY_ENTRIES = 32


def memory_delta_error(delta):
    """Return a safe error without copying rejected memory into diagnostics."""
    if not isinstance(delta, dict):
        return "memory_delta must be an object"
    entries = delta.get("entries", [])
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        return "memory_delta.entries must be a list of objects"
    if len(entries) > MAX_MEMORY_ENTRIES:
        return "memory_delta exceeds the 32-entry limit"
    try:
        encoded = json.dumps(delta, ensure_ascii=False, allow_nan=False)
        size = len(encoded.encode("utf-8"))
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return "memory_delta must contain valid JSON"
    if size > MAX_MEMORY_BYTES:
        return "memory_delta exceeds the 64 KiB limit"
    pending = [encoded, delta]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str):
            checked = redact_text(value)
            if not checked["ok"]:
                return "memory_delta contains a high-risk secret pattern"
    return None


def redact_memory_delta(delta):
    """Return a safe copy without turning redactable context into a failed task."""
    error = memory_delta_error(delta)
    if error:
        raise ValueError(error)

    def redact(value):
        if isinstance(value, dict):
            return {redact(key): redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, str):
            return redact_text(value)["text"]
        return value

    redacted = redact(delta)
    if len(json.dumps(redacted, ensure_ascii=False).encode("utf-8")) > MAX_MEMORY_BYTES:
        return {"status": "none", "reason": "Memory omitted because redacted content exceeds the 64 KiB limit."}
    return redacted
