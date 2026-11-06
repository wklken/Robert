"""Safe text shared by operator commands and read models."""
from robert_agent.redaction import redact_and_truncate


def safe_text(value, limit=1000):
    if not isinstance(value, str):
        return ""
    result = redact_and_truncate(value, limit)
    return result.get("text", "Sensitive output hidden; inspect the local worker log.")
