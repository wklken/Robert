"""Redaction helpers for the rewritten DD GitHub agent."""

import re
from bisect import bisect_right

MASKED_VALUE_PATTERN = r"(?:<redacted>|\[redacted\]|\.\.\.)"

SECRET_PATTERNS = [
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
_SECRET_ASSIGNMENT_PREFIX = (
    r"(?<![A-Za-z0-9_])(?<![A-Za-z0-9]-)\b(?P<name>(?:[A-Za-z0-9]+[_-])*(?:token|password|passwd|api[_-]?key|secret(?:[_-](?:access[_-])?key)?|private[_-]key|accessToken|refreshToken))\b[\"']?"
    r"\s*[:=](?!=)\s*(?:[\"'](?P<quoted>[^\"'\r\n]+)[\"']|"
)
_MASKED_ASSIGNMENT = rf"(?P<masked>{MASKED_VALUE_PATTERN})(?=$|[\s`,;&)\]}}])|"
SECRET_ASSIGNMENT = re.compile(
    _SECRET_ASSIGNMENT_PREFIX + _MASKED_ASSIGNMENT + r"(?P<bare>(?:<redacted>|\[redacted\])?[A-Za-z0-9_./+=$-]+))", re.I
)
# Diagnostics are not source snippets: accept punctuation/Unicode in bare
# values, stopping at surrounding prose/URL delimiters rather than matching
# a harmless-looking prefix such as `none` in `password=none!`.
DIAGNOSTIC_SECRET_ASSIGNMENT = re.compile(
    _SECRET_ASSIGNMENT_PREFIX + _MASKED_ASSIGNMENT + r"(?P<bare>[^\s\"'`,;&][^\s\"'`,;&)\]}]*))",
    re.I,
)
PLACEHOLDER_VALUES = {
    "none",
    "null",
    "true",
    "false",
    "...",
    "example",
    "placeholder",
    "redacted",
    "<redacted>",
    "[redacted]",
}

COOKIE_HEADER = re.compile(r"(?:Set-)?Cookie\s*:", re.I)
_AUTHORIZATION_HEADER = re.compile(r"Authorization\s*:", re.I)
_COOKIE_PLACEHOLDER = MASKED_VALUE_PATTERN
_COOKIE_VALUE = rf"(?:{_COOKIE_PLACEHOLDER}|\"{_COOKIE_PLACEHOLDER}\"|'{_COOKIE_PLACEHOLDER}')"
_COOKIE_PAIR = re.compile(rf"[A-Za-z0-9_.-]+[ \t]*=[ \t]*{_COOKIE_VALUE}", re.I)
_SET_COOKIE_ATTRIBUTE = re.compile(
    r"(?:Path[ \t]*=[ \t]*/|SameSite[ \t]*=[ \t]*(?:Lax|Strict|None)|Secure|HttpOnly|Partitioned)", re.I
)
# Only clearly separated Chinese prose can follow an inline example. Latin
# tokens, digits and assignment/header delimiters keep the whole header blocked.
_COOKIE_PROSE = re.compile(r"(?P<header>.*?)[，。][\u3400-\u9fff \t，。！？（）「」“”]*", re.S)


def _header_end(text, value_start):
    end = text.find("\n", value_start)
    if end == -1:
        return len(text)
    previous = text[value_start:end].rstrip()
    while end < len(text):
        next_end = text.find("\n", end + 1)
        if next_end == -1:
            next_end = len(text)
        line = text[end + 1 : next_end]
        # HTTP folds use indentation. Also recognize examples wrapped after
        # a semicolon or an empty header, even without indentation.
        if not line.strip() or re.match(r"[ \t]*(?:Authorization|(?:Set-)?Cookie)\s*:", line, re.I):
            break
        if not line.startswith((" ", "\t")) and previous and not previous.endswith(";"):
            break
        end = next_end
        previous = line.rstrip()
    return end


def _redacted_cookie_header(text, match, end):
    raw_value = text[match.end() : end]
    value = re.sub(r"\r?\n[ \t]*", " ", raw_value).strip()
    prose = _COOKIE_PROSE.fullmatch(value)
    if prose:
        # Inline explanation cannot turn a folded credential into prose.
        if "\n" in raw_value:
            return False
        value = prose["header"].rstrip()
    # A report may quote a complete example. Require the closing delimiter;
    # never discard arbitrary text appended after it.
    wrapper = text[match.start() - 1] if match.start() else ""
    if wrapper in ("`", '"', "'"):
        if not value.endswith(wrapper):
            return False
        value = value[:-1].rstrip()
    parts = [part.strip() for part in value.split(";")]
    if not (re.fullmatch(_COOKIE_VALUE, parts[0], re.I) or _COOKIE_PAIR.fullmatch(parts[0])):
        return False
    is_set_cookie = match.group().lower().startswith("set-cookie")
    return all(
        _COOKIE_PAIR.fullmatch(part) or (is_set_cookie and _SET_COOKIE_ATTRIBUTE.fullmatch(part)) for part in parts[1:]
    )


def _finding(text, start, rule):
    return {
        "rule": rule,
        "line": text.count("\n", 0, start) + 1,
        "column": start - text.rfind("\n", 0, start),
    }


def _synthetic_password(text, match):
    if match["name"].lower() != "password" or (match["quoted"] or match["masked"] or match["bare"]).lower() != "secret":
        return False
    # A partial bare match (e.g. secret!suffix) must remain blocked.
    return bool(match["quoted"] or match.end() == len(text) or text[match.end()] in " \t\r\n&,;`)]}")


def _environment_reference(match, strict_assignments):
    value = match["bare"]
    if not value:
        return False
    if not strict_assignments:
        return value.startswith(("$", "os.environ", "os.getenv"))
    # Broad diagnostic values must not exempt appended credentials merely
    # because they begin with an environment reference. Function/index forms
    # stop at the quoted variable name; shell forms must be a single variable.
    return re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*|os\.environ(?:\[|\.get\()|os\.getenv\(", value) is not None


LOCAL_PATH = re.compile(r"(?<![\w.-])(?:/Users|/home|/root|/data|/tmp|/var/folders)/[^\s:;,)]+")
INTERNAL_IP = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
)
INTERNAL_DOMAIN = re.compile(r"(?<![A-Za-z0-9.-])\b[A-Za-z0-9.-]+\.(?:woa\.com|corp|internal|local)\b")
# Userinfo is sensitive regardless of credential length or percent encoding.
# Stop at authority boundaries, but allow punctuation and embedded @ in credentials.
URL_USERINFO = re.compile(r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://[^\s/?#]*@")


def _scan_secrets(text, *, strict_assignments):
    spans = []
    replacements = []
    assignment_pattern = DIAGNOSTIC_SECRET_ASSIGNMENT if strict_assignments else SECRET_ASSIGNMENT
    for match in assignment_pattern.finditer(text):
        value = match["quoted"] or match["masked"] or match["bare"]
        if _synthetic_password(text, match):
            replacements.append(match.span("quoted" if match["quoted"] else "bare"))
        elif (
            value.lower() not in PLACEHOLDER_VALUES
            and not _environment_reference(match, strict_assignments)
            and (strict_assignments or len(value) >= 6)
        ):
            spans.append((*match.span(), "credential_assignment"))
    for match in COOKIE_HEADER.finditer(text):
        end = _header_end(text, match.end())
        if not _redacted_cookie_header(text, match, end):
            spans.append((match.start(), end, "high_risk_secret"))
    for match in _AUTHORIZATION_HEADER.finditer(text):
        end = _header_end(text, match.end())
        value = re.sub(r"\r?\n[ \t]*", " ", text[match.end() : end]).strip()
        wrapper = text[match.start() - 1] if match.start() else ""
        if wrapper in ("`", '"', "'") and value.endswith(wrapper):
            value = value[:-1].rstrip()
        if not re.fullmatch(
            rf"(?:none|null|{MASKED_VALUE_PATTERN}|[A-Za-z][A-Za-z0-9_-]*[ \t]+{MASKED_VALUE_PATTERN})",
            value,
            re.I,
        ):
            spans.append((match.start(), end, "high_risk_secret"))
    for pattern in SECRET_PATTERNS:
        spans.extend((*match.span(), "high_risk_secret") for match in pattern.finditer(text))
    spans.extend((*match.span(), "url_userinfo") for match in URL_USERINFO.finditer(text))
    return spans, replacements


def _redact_text(text, *, strict_assignments):
    spans, replacements = _scan_secrets(text, strict_assignments=strict_assignments)
    if spans:
        return {
            "ok": False,
            "status": "blocked_secret",
            "safe_error": "text contains a high-risk secret pattern",
            "findings": sorted(
                (_finding(text, start, rule) for start, end, rule in spans),
                key=lambda item: (item["line"], item["column"], item["rule"]),
            ),
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


def redact_text(text, *, strict_assignments=False):
    return _redact_text(text, strict_assignments=strict_assignments)


def redact_report(text):
    """Create a publication-safe report copy; keep sealed evidence separately.

    Remove complete physical lines touched by secrets, including folded header
    values and entire private-key blocks. Other scanners still inspect the
    original structures; a redacted cookie never exempts an unrelated secret.
    Complete cookie placeholder examples share the global masked-value policy,
    including the strict final scan of the publication copy.
    """
    spans, _ = _scan_secrets(text, strict_assignments=True)
    lines = text.splitlines(keepends=True)
    offsets = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    unsafe_lines = set()
    for start, end, rule in spans:
        if rule == "high_risk_secret" and text[start:end].lower().startswith("authorization"):
            end = _header_end(text, text.find(":", start, end) + 1)
        elif rule == "high_risk_secret" and text.startswith("-----BEGIN ", start):
            closing = text[start:end].replace("-----BEGIN ", "-----END ", 1)
            closing_start = text.find(closing, end)
            # A truncated key has no reliable safe boundary; remove its tail.
            end = len(text) if closing_start == -1 else closing_start + len(closing)
        unsafe_lines.update(range(bisect_right(offsets, start) - 1, bisect_right(offsets, end - 1)))
    for index in unsafe_lines:
        line = lines[index]
        ending = line[len(line.rstrip("\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029")) :]
        lines[index] = "[redacted sensitive report line]" + ending
    return _redact_text("".join(lines), strict_assignments=True)


def redact_and_truncate(text, max_chars, *, strict_assignments=False):
    result = redact_text(text, strict_assignments=strict_assignments)
    if not result["ok"]:
        return result
    redacted = result["text"]
    return {
        **result,
        "text": redacted[:max_chars],
        "truncated": len(redacted) > max_chars,
    }
