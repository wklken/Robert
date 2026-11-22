"""Safe text shared by operator commands and read models."""

import re
from urllib.parse import unquote, urlsplit, urlunsplit

from robert_agent.security.redaction import redact_and_truncate, redact_text


ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def strip_ansi(text):
    return ANSI_ESCAPE_RE.sub("", text)


def safe_text(value, limit=1000):
    if not isinstance(value, str):
        return ""
    result = redact_and_truncate(value, limit)
    return result.get("text", "Sensitive output hidden; inspect the local worker log.")


def safe_public_link(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or any(char.isspace() or ord(char) < 32 or char in "<>\\" for char in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username is not None:
            return None
        # Queries can carry access credentials. Stored issue, PR and TAPD
        # permalinks identify the destination with their path and fragment.
        url = urlunsplit(parsed._replace(query=""))
    except ValueError:
        return None
    return url if redact_text(unquote(url), strict_assignments=True)["ok"] else None
