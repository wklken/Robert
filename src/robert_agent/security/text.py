"""Safe text shared by operator commands and read models."""

import re

from robert_agent.security.redaction import redact_and_truncate


ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def strip_ansi(text):
    return ANSI_ESCAPE_RE.sub("", text)


def safe_text(value, limit=1000):
    if not isinstance(value, str):
        return ""
    result = redact_and_truncate(value, limit)
    return result.get("text", "Sensitive output hidden; inspect the local worker log.")
