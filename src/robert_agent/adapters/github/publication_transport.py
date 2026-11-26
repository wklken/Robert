"""Capture publication transport evidence without changing command authority.

The injected runner owns process execution. This adapter does no retries or
persistence; callers can store diagnostics and consume facts from the last call.
"""

from datetime import datetime, timezone
import errno
import json
import math
import os
import re
import shlex
import subprocess
import time

from robert_agent.security.redaction import redact_report
from robert_agent.security.text import strip_ansi

_MAX_ENTRIES = 12
_MAX_EXCERPT = 4096
_MASK = "<redacted>"
_HEADERS = frozenset(
    {
        "retry-after",
        "x-ratelimit-reset",
        "x-ratelimit-remaining",
        "x-ratelimit-resource",
        "x-github-request-id",
        "date",
        "content-type",
    }
)
_ERROR_CLASSES = frozenset(
    {
        "network_failure",
        "rate_limited",
        "authentication_required",
        "permission_required",
        "configuration_required",
    }
)
_EXCEPTIONS = frozenset(
    {
        "TimeoutExpired",
        "CalledProcessError",
        "OSError",
        "PermissionError",
        "FileNotFoundError",
        "ConnectionResetError",
        "ConnectionRefusedError",
        "TimeoutError",
        "BrokenPipeError",
        "ConnectionAbortedError",
    }
)
_EXCERPTS = ("request_excerpt", "response_excerpt", "stdout_excerpt", "stderr_excerpt")
_COMMAND_WORDS = frozenset(
    {"pr", "api", "list", "create", "view", "edit", "comment", "merge", "close", "reopen", "push", "fetch", "ls-remote"}
)
_FLAGS = frozenset(
    {
        "--repo",
        "-R",
        "--json",
        "--jq",
        "--template",
        "--body",
        "--body-file",
        "--title",
        "--head",
        "--base",
        "--state",
        "--limit",
        "--search",
        "--author",
        "--assignee",
        "--label",
        "--reviewer",
        "--draft",
        "--fill",
        "--method",
        "-X",
        "--header",
        "-H",
        "--field",
        "-f",
        "--raw-field",
        "-F",
        "--input",
        "--hostname",
        "--paginate",
        "--slurp",
        "--silent",
        "--include",
        "--force-with-lease",
        "--porcelain",
        "--set-upstream",
        "--delete",
        "--dry-run",
        "--tags",
        "--no-tags",
        "--prune",
        "--",
        "-u",
    }
)
_STATUS = re.compile(r"^< HTTP/\S+\s+(\d{3})(?:\s|$)")
_REQUEST = re.compile(r"^> [A-Z]+\s+\S+\s+HTTP/\S+")
_CLI_STATUS = re.compile(r"\(HTTP\s+(\d{3})\)|(?:requested URL returned error:|HTTP(?:/\S+)?[ :]+)\s*(\d{3})\b", re.I)
_CLI_START = re.compile(r"^(?:gh:|fatal:|error:|remote:|git@|To )", re.I)
_RATE_LIMIT = re.compile(
    r"(?:API\s+)?rate limit exceeded|exceeded (?:a |the )?(?:secondary |API )?rate limit|secondary rate limit|rate limited",
    re.I,
)
_AUTH = re.compile(
    r"authentication failed|bad credentials|requires authentication|could not read (?:Username|Password)|authentication required",
    re.I,
)
_PERMISSION = re.compile(
    r"permission denied|permission to .+ denied|access denied|resource not accessible|write access .+ not granted", re.I
)
_NETWORK = re.compile(
    r"connection (?:reset|refused)|(?:operation |connection |handshake |i/o )?timed? out|timeout|"
    r"could not resolve (?:host|hostname)|no such host|temporary failure in name resolution|"
    r"name or service not known|network is unreachable|network unreachable|no route to host|"
    r"(?:^|[\s:])(?:unexpected )?EOF(?:$|[\s.,])|unexpected end of JSON input|"
    r"TLS (?:handshake|error)|SSL (?:certificate problem|connect error)|certificate (?:verify failed|has expired)|"
    r"x509:|remote error: tls:",
    re.I,
)
_NETWORK_ERRNOS = frozenset(
    {
        errno.ECONNRESET,
        errno.ECONNREFUSED,
        errno.ETIMEDOUT,
        errno.ENETUNREACH,
        errno.EHOSTUNREACH,
        errno.ECONNABORTED,
        errno.EPIPE,
    }
)
_JSON_START = re.compile(r"[\[{]")
_JSON_KEY = re.compile(r'"((?:[^"\\]|\\.)*)"\s*:\s*')


def _sensitive_key(key):
    compact = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(
        word in compact
        for word in (
            "token",
            "password",
            "passwd",
            "secret",
            "cookie",
            "authorization",
            "credential",
            "privatekey",
            "apikey",
        )
    )


def _safe_json(value, depth):
    if depth > 40:
        return _MASK
    if isinstance(value, dict):
        return {key: _MASK if _sensitive_key(key) else _safe_json(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_json(item, depth + 1) for item in value]
    if isinstance(value, str):
        return _safe_text(value, depth + 1)
    return value


def _safe_text(value, depth=0):
    """Redact complete JSON structurally and uncertain sensitive tails in full."""
    if depth > 40:
        return _MASK
    text = strip_ansi(value)
    decoder = json.JSONDecoder()
    parts = []
    end = 0
    for match in _JSON_START.finditer(text):
        if match.start() < end:
            continue
        try:
            item, length = decoder.raw_decode(text[match.start() :])
            replacement = json.dumps(_safe_json(item, depth + 1), ensure_ascii=False)
        except (ValueError, RecursionError):
            partial = text[match.start() :]
            json_prefix = partial.startswith("{") or (
                partial.startswith("[") and partial[1:].lstrip()[:1] in '[{"-0123456789tfn'
            )
            if json_prefix and any(_sensitive_key(key[1]) for key in _JSON_KEY.finditer(partial)):
                parts.extend((text[end : match.start()], "[redacted incomplete JSON body]"))
                end = len(text)
                break
            continue
        parts.extend((text[end : match.start()], replacement))
        end = match.start() + length
    parts.append(text[end:])
    text = "".join(parts)
    # A partial body may end in the middle of a string, object or array. There
    # is no trustworthy value boundary there; discard its entire remaining tail.
    for match in _JSON_KEY.finditer(text):
        try:
            key = json.loads('"' + match[1] + '"')
        except ValueError:
            continue
        if _sensitive_key(key) and not text[match.end() :].startswith('"' + _MASK + '"'):
            text = text[: match.end()] + json.dumps(_MASK)
            break
    safe = redact_report(text)
    return safe.get("text", "[redacted sensitive output]")


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


def _command_parts(command):
    if isinstance(command, str):
        try:
            return shlex.split(command)
        except ValueError:
            return []
    if isinstance(command, (list, tuple)):
        return [os.fspath(item) if isinstance(item, os.PathLike) else item for item in command]
    return []


def _safe_command(command):
    parts = _command_parts(command)
    if not parts or not all(isinstance(item, str) for item in parts):
        return []
    program = os.path.basename(parts[0])
    result = [program if program in {"gh", "git"} else _MASK]
    prefix = True
    for item in parts[1:]:
        if prefix and item in _COMMAND_WORDS:
            result.append(item)
        else:
            prefix = False
            flag, separator, _ = item.partition("=")
            result.append(flag + ("=" + _MASK if separator else "") if flag in _FLAGS else _MASK)
    return result[:64]


def _safe_headers(headers):
    if not isinstance(headers, dict):
        return {}
    return {
        key.lower(): _safe_text(value)[:512]
        for key, value in headers.items()
        if isinstance(key, str)
        and key.lower() in _HEADERS
        and isinstance(value, str)
        and "\n" not in value
        and "\r" not in value
    }


def safe_diagnostics(entries):
    """Revalidate an untrusted persisted journal; return only bounded safe fields."""
    if not isinstance(entries, list):
        return []
    output = []
    for entry in entries[-_MAX_ENTRIES:]:
        if not isinstance(entry, dict) or not isinstance(entry.get("command"), list):
            continue
        command = _safe_command(entry["command"])
        returncode = entry.get("returncode")
        exception_type = entry.get("exception_type")
        known_exception = isinstance(exception_type, str) and exception_type in _EXCEPTIONS
        if not command or not (type(returncode) is int or (returncode is None and known_exception)):
            continue
        safe = {"command": command, "returncode": returncode}
        if known_exception:
            safe["exception_type"] = exception_type
        if isinstance(entry.get("error_class"), str) and entry["error_class"] in _ERROR_CLASSES:
            safe["error_class"] = entry["error_class"]
        status = entry.get("http_status")
        if type(status) is int and 100 <= status <= 599:
            safe["http_status"] = status
        elapsed = entry.get("elapsed_seconds")
        if type(elapsed) in (int, float) and 0 <= elapsed <= 1e9 and math.isfinite(elapsed):
            safe["elapsed_seconds"] = elapsed
        timestamp = entry.get("timestamp")
        if isinstance(timestamp, str) and len(timestamp) <= 64:
            try:
                parsed = datetime.fromisoformat(timestamp)
                if parsed.tzinfo is not None:
                    safe["timestamp"] = parsed.astimezone(timezone.utc).isoformat()
            except (ValueError, OverflowError):
                pass
        for key in _EXCERPTS:
            if isinstance(entry.get(key), str):
                safe[key] = _safe_text(entry[key])[:_MAX_EXCERPT]
        safe["response_headers"] = _safe_headers(entry.get("response_headers"))
        output.append(safe)
    return output


def _parse_trace(stderr):
    """Read gh's httpretty format, keeping the final HTTP exchange's facts."""
    errors, request, response, body = [], [], [], []
    status, headers, section = None, {}, None
    for line in strip_ansi(stderr).splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith(("* Request at ", "* Request to ")):
            section = "request"
            continue
        if stripped.startswith("* Request took "):
            section = None
            continue
        status_match = _STATUS.match(stripped)
        if status_match:
            status, headers, section = int(status_match[1]), {}, "response"
            response, body = [line], []
            continue
        if _REQUEST.match(stripped):
            section = "request"
        if section == "request" and stripped.startswith("> "):
            request.append(line)
        elif section == "response" and stripped.startswith("< "):
            response.append(line)
            key, separator, value = stripped[2:].partition(":")
            if separator and key.lower() in _HEADERS:
                headers[key.lower()] = value.strip()
        elif section and not _CLI_START.match(stripped) and not stripped.lower().startswith("warning:"):
            if section == "request":
                request.append(line)
            else:
                response.append(line)
                body.append(line)
        else:
            section = None
            errors.append(line)
    return "".join(errors), "".join(request), "".join(response), status, headers, "".join(body)


def _error_lines(stderr):
    return "\n".join(
        line
        for line in stderr.splitlines()
        if not line.lstrip().lower().startswith(("warning:", "warn:", "debug:", "* ", "> ", "< "))
    )


def _malformed_api_json(command, stdout, headers):
    parts = _command_parts(command)
    if "api" not in parts[1:3] or not all(isinstance(part, str) for part in parts):
        return False
    flags = {part.partition("=")[0] for part in parts}
    if flags & {"--jq", "-q", "--template", "-t", "--include", "-i", "--silent"}:
        return False
    text = _text(stdout).strip()
    if not text:
        return False
    if text[0] not in "[{" and "json" not in headers.get("content-type", "").lower():
        return False
    decoder = json.JSONDecoder()
    try:
        while text:
            _, end = decoder.raw_decode(text)
            text = text[end:].lstrip()
        return False
    except (ValueError, RecursionError):
        return True


def _failure_facts(returncode, stderr, status, headers, body, exception):
    if returncode == 0 and exception is None:
        return {}
    message = _error_lines(stderr)
    if status is None:
        matches = list(_CLI_STATUS.finditer(message))
        if matches:
            status = int(matches[-1][1] or matches[-1][2])
    facts = {"response_headers": _safe_headers(headers)}
    if status is not None:
        facts["http_status"] = status
    response_message = ""
    try:
        value = json.loads(body)
        if isinstance(value, dict) and isinstance(value.get("message"), str):
            response_message = value["message"]
    except (ValueError, RecursionError):
        pass
    rate_limited = bool(_RATE_LIMIT.search(response_message) or _RATE_LIMIT.search(message))
    error_class = None
    if status == 401:
        error_class = "authentication_required"
    elif status == 429 or (
        status == 403 and ("retry-after" in headers or headers.get("x-ratelimit-remaining") == "0" or rate_limited)
    ):
        error_class = "rate_limited"
    elif status in {403, 404}:
        error_class = "permission_required"
    elif status == 422:
        error_class = "configuration_required"
    elif status is not None and 500 <= status <= 599:
        error_class = "network_failure"
    elif status is not None and 400 <= status <= 499:
        pass
    elif isinstance(exception, subprocess.TimeoutExpired):
        error_class = "network_failure"
    elif isinstance(exception, OSError):
        if exception.errno in _NETWORK_ERRNOS:
            error_class = "network_failure"
        elif exception.errno in {errno.EACCES, errno.EPERM}:
            error_class = "permission_required"
    elif _AUTH.search(message):
        error_class = "authentication_required"
    elif _PERMISSION.search(message):
        error_class = "permission_required"
    elif status is None and _RATE_LIMIT.search(message):
        error_class = "rate_limited"
    elif _NETWORK.search(message):
        error_class = "network_failure"
    if error_class:
        facts["error_class"] = error_class
    return facts


class PublicationTransport:
    """Runner compatible callable with a safe journal and last-call failure facts."""

    def __init__(self, runner):
        self.runner = runner
        self.diagnostics = []
        self._facts = {}

    def __call__(self, command, **kwargs):
        parts = _command_parts(command)
        is_gh = bool(parts and isinstance(parts[0], str) and os.path.basename(parts[0]) == "gh")
        if is_gh:
            kwargs["env"] = {**os.environ, **(kwargs.get("env") or {}), "GH_DEBUG": "api"}
        if "capture_output" not in kwargs and "stdout" not in kwargs and "stderr" not in kwargs:
            kwargs["capture_output"] = True
        timestamp = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        self._facts = {}
        try:
            result = self.runner(command, **kwargs)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError) as exc:
            stdout = getattr(exc, "output", None)
            stderr = getattr(exc, "stderr", None)
            if isinstance(exc, OSError):
                # str(exc) includes filenames and arbitrary runner details.
                stderr = os.strerror(exc.errno) if isinstance(exc.errno, int) else "Operating system error"
            safe_stderr, _ = self._record(
                command, stdout, stderr, getattr(exc, "returncode", None), is_gh, timestamp, started, exc
            )
            if hasattr(exc, "stderr"):
                exc.stderr = safe_stderr
            if isinstance(stdout, (str, bytes)):
                safe_stdout = _safe_text(_text(stdout))
                exc.output = safe_stdout.encode("utf-8") if isinstance(stdout, bytes) else safe_stdout
            raise
        safe_stderr, returncode = self._record(
            command,
            getattr(result, "stdout", None),
            getattr(result, "stderr", None),
            result.returncode,
            is_gh,
            timestamp,
            started,
        )
        if kwargs.get("check") and returncode != 0:
            raise subprocess.CalledProcessError(
                returncode, command, _safe_text(_text(getattr(result, "stdout", None))), safe_stderr
            )
        return subprocess.CompletedProcess(command, returncode, getattr(result, "stdout", None), safe_stderr)

    def _record(self, command, stdout, stderr, returncode, is_gh, timestamp, started, exception=None):
        raw_stderr = _text(stderr)
        if is_gh:
            error, request, response, status, headers, body = _parse_trace(raw_stderr)
        else:
            error, request, response, status, headers, body = raw_stderr, "", "", None, {}, ""
        effective_code = returncode
        if returncode == 0 and is_gh and _malformed_api_json(command, stdout, headers):
            effective_code = 1
            error += (
                "\n" if error and not error.endswith("\n") else ""
            ) + "gh: unexpected end of JSON input or invalid API JSON response\n"
        self._facts = _failure_facts(effective_code, error, status, headers, body, exception)
        entry = {
            "command": _safe_command(command),
            "returncode": returncode,
            "timestamp": timestamp,
            "elapsed_seconds": max(0, time.monotonic() - started),
            "request_excerpt": _safe_text(request)[:_MAX_EXCERPT],
            "response_excerpt": _safe_text(response)[:_MAX_EXCERPT],
            "stdout_excerpt": _safe_text(_text(stdout))[:_MAX_EXCERPT],
            "stderr_excerpt": _safe_text(error)[:_MAX_EXCERPT],
            "response_headers": _safe_headers(headers),
            **self._facts,
        }
        if exception is not None:
            entry["exception_type"] = (
                type(exception).__name__
                if type(exception).__name__ in _EXCEPTIONS
                else ("TimeoutExpired" if isinstance(exception, subprocess.TimeoutExpired) else "OSError")
            )
        self.diagnostics.append(entry)
        del self.diagnostics[:-_MAX_ENTRIES]
        safe_error = _safe_text(error)
        return (safe_error.encode("utf-8") if isinstance(stderr, bytes) else safe_error), effective_code

    def failure_facts(self):
        """Return independent facts for the most recent call, empty on success."""
        return {key: dict(value) if isinstance(value, dict) else value for key, value in self._facts.items()}
