"""Redaction helpers for the rewritten DD GitHub agent."""

import re


SECRET_PATTERNS = [
    re.compile(r"Authorization\s*:", re.I),
    re.compile(r"Cookie\s*:", re.I),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b[A-Za-z0-9_]*TOKEN[A-Za-z0-9_]*\s*=", re.I),
    re.compile(r"\btoken\s*:\s*(?:ghp|github_pat|gho|ghu|ghs|ghr)_[A-Za-z0-9_]+", re.I),
    re.compile(r"\b(?:ghp|github_pat|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b", re.I),
]

LOCAL_PATH = re.compile(r"(?:/Users|/home|/root|/data|/tmp|/var/folders)/[^\s:;,)]+")
INTERNAL_IP = re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")
INTERNAL_DOMAIN = re.compile(r"\b[A-Za-z0-9.-]+\.(?:woa\.com|corp|internal|local)\b")


def redact_text(text):
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
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
