"""Redaction helpers for the rewritten DD GitHub agent."""

import re


SECRET_PATTERNS = [
    re.compile(r"Authorization\s*:(?![ \t]*<redacted>[ \t]*(?:\r?\n|$))[ \t]*(?!(?:none|null|\.\.\.)(?:$|[\s,;]))[^\s,;]+", re.I),
    re.compile(r"Cookie\s*:(?![ \t]*<redacted>[ \t]*(?:\r?\n|$))", re.I),
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
    r"\b(?P<name>(?:[A-Za-z0-9]+_)*(?:token|password|passwd|api_key|secret))\b"
    r"\s*[:=](?!=)\s*(?:[\"'](?P<quoted>[^\"'\r\n]+)[\"']|(?P<bare>[A-Za-z0-9_./+=$-]+))",
    re.I,
)
PLACEHOLDER_VALUES = {"none", "null", "true", "false", "...", "example", "placeholder", "redacted", "<redacted>"}


def _finding(text, match, rule):
    start = match.start()
    return {"rule": rule, "line": text.count("\n", 0, start) + 1,
            "column": start - text.rfind("\n", 0, start)}


def _synthetic_password(text, match):
    if match["name"].lower() != "password" or (match["quoted"] or match["bare"]).lower() != "secret":
        return False
    # A partial bare match (e.g. secret!suffix) must remain blocked.
    return bool(match["quoted"] or match.end() == len(text)
                or text[match.end()] in " \t\r\n&,;`)]}")


LOCAL_PATH = re.compile(r"(?<![\w.-])(?:/Users|/home|/root|/data|/tmp|/var/folders)/[^\s:;,)]+")
INTERNAL_IP = re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")
INTERNAL_DOMAIN = re.compile(r"\b[A-Za-z0-9.-]+\.(?:woa\.com|corp|internal|local)\b")


def redact_text(text):
    findings = []
    replacements = []
    for match in SECRET_ASSIGNMENT.finditer(text):
        value = match["quoted"] or match["bare"]
        if _synthetic_password(text, match):
            replacements.append(match.span("quoted" if match["quoted"] else "bare"))
        elif (value.lower() not in PLACEHOLDER_VALUES
              and not (match["bare"] and value.startswith(("$", "os.environ", "os.getenv")))
              and len(value) >= 6):
            findings.append(_finding(text, match, "credential_assignment"))
    for pattern in SECRET_PATTERNS:
        findings.extend(_finding(text, match, "high_risk_secret") for match in pattern.finditer(text))
    if findings:
        return {
            "ok": False,
            "status": "blocked_secret",
            "safe_error": "text contains a high-risk secret pattern",
            "findings": sorted(findings, key=lambda item: (item["line"], item["column"], item["rule"])),
        }

    redacted = text
    for start, end in reversed(replacements):
        redacted = redacted[:start] + "<redacted>" + redacted[end:]
    redacted = LOCAL_PATH.sub("<local-path>", redacted)
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
