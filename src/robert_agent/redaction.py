"""Redaction helpers for the rewritten DD GitHub agent."""

import re


SECRET_PATTERNS = [
    re.compile(r"Authorization\s*:[ \t]*(?!(?:none|null|\.\.\.)(?:$|[\s,;]))[^\s,;]+", re.I),
    re.compile(r"Cookie\s*:", re.I),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\btoken\s*:\s*(?:ghp|github_pat|gho|ghu|ghs|ghr)_[A-Za-z0-9_]+", re.I),
    re.compile(r"\b(?:ghp|github_pat|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b", re.I),
]

# Match credential assignments, not names such as max_tokens or comparisons.
SECRET_ASSIGNMENT = re.compile(
    r"\b(?:[A-Za-z0-9]+_)*(?:token|password|passwd|api_key|secret)\b"
    r"\s*[:=](?!=)\s*(?:[\"'](?P<quoted>[^\"'\r\n]+)[\"']|(?P<bare>[A-Za-z0-9_./+=$-]+))",
    re.I,
)
PLACEHOLDER_VALUES = {"none", "null", "true", "false", "...", "example", "placeholder", "redacted"}


def _has_secret_assignment(text):
    for match in SECRET_ASSIGNMENT.finditer(text):
        value = match["quoted"] or match["bare"]
        if value.lower() in PLACEHOLDER_VALUES:
            continue
        if match["bare"] and value.startswith(("$", "os.environ", "os.getenv")):
            continue
        if len(value) >= 6:
            return True
    return False


LOCAL_PATH = re.compile(r"(?<![\w.-])(?:/Users|/home|/root|/data|/tmp|/var/folders)/[^\s:;,)]+")
INTERNAL_IP = re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")
INTERNAL_DOMAIN = re.compile(r"\b[A-Za-z0-9.-]+\.(?:woa\.com|corp|internal|local)\b")


def redact_text(text):
    if _has_secret_assignment(text) or any(pattern.search(text) for pattern in SECRET_PATTERNS):
        return {
            "ok": False,
            "status": "blocked_secret",
            "safe_error": "text contains a high-risk secret pattern",
        }

    redacted = LOCAL_PATH.sub("<local-path>", text)
    redacted = INTERNAL_IP.sub("<internal-ip>", redacted)
    redacted = INTERNAL_DOMAIN.sub("<internal-domain>", redacted)
    return {
        "ok": True,
        "status": "redacted",
        "text": redacted,
    }


def redact_and_truncate(text, max_chars):
    result = redact_text(text)
    if not result["ok"]:
        return result
    redacted = result["text"]
    return {
        **result,
        "text": redacted[:max_chars],
        "truncated": len(redacted) > max_chars,
    }
