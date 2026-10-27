"""Bounded retry decisions shared by publication and durable delivery."""

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import re
import json

MAX_ATTEMPTS = 3
MAX_ELAPSED_SECONDS = 600


def error_class(message):
    text = str(message or "").lower()
    if re.search(
        r"\b401\b|unauthorized|auth expired|authentication failed|invalid (?:api )?key|not logged in",
        text,
    ):
        return "authentication_required"
    if re.search(r"\b429\b|rate.?limit|too many requests|secondary rate|abuse detection", text):
        return "rate_limited"
    if re.search(r"\b403\b|\bforbidden\b|resource not accessible by", text):
        return "permission_required"
    if any(
        value in text
        for value in (
            "non-fast-forward",
            "remote_sha_conflict",
            "branch diverged",
            "stale info",
        )
    ):
        return "branch_conflict"
    if any(
        value in text
        for value in (
            "permission denied",
            "permission to ",
            "protected branch",
            "repository not found",
        )
    ):
        return "permission_required"
    if any(
        value in text
        for value in ("missing fields", "not a valid", "invalid configuration", "src refspec", "does not appear to be a git repository")
    ):
        return "configuration_required"
    return "transient_failure"


def timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _retry_after_seconds(message, now):
    match = re.search(r"(?im)\bretry-after\s*:\s*([^\r\n]+)", str(message or ""))
    if not match:
        return 0
    value = match.group(1).strip()
    if value.isdecimal():
        # Avoid unbounded integer input while still stopping beyond the window.
        return int(value[:10]) if len(value) <= 10 else MAX_ELAPSED_SECONDS + 1
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0, (date - timestamp(now)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return 0


def failure_state(previous, message, now, *, retryable=True):
    count = int(previous.get("attempt_count", 0)) + 1
    first = previous.get("first_failed_at") or now
    category = error_class(message)
    elapsed = (timestamp(now) - timestamp(first)).total_seconds()
    transient = category in {"transient_failure", "rate_limited"}
    delay = max(min(60, 5 * 2 ** min(count - 1, 4)), _retry_after_seconds(message, now))
    allowed = (
        retryable
        and transient
        and count < MAX_ATTEMPTS
        and elapsed + delay < MAX_ELAPSED_SECONDS
    )
    reason = (
        category
        if not transient
        else ("retry_scheduled" if allowed else "retry_budget_exhausted")
    )
    return {
        "attempt_count": count,
        "max_attempts": MAX_ATTEMPTS,
        "first_failed_at": first,
        "failed_at": now,
        "error_class": category,
        "reason_code": reason,
        "retryable": bool(allowed),
        "next_retry_at": (
            timestamp(now) + timedelta(seconds=delay)
        ).isoformat()
        if allowed
        else None,
        "required_action": None
        if allowed
        else {
            "authentication_required": "Repair credentials, then retry the task.",
            "permission_required": "Repair repository permissions, then retry the task.",
            "branch_conflict": "Review the current branch and obtain a new audited result.",
            "configuration_required": "Repair configuration, then retry the task.",
        }.get(
            category,
            "Inspect the failure and explicitly retry after resolving its cause.",
        ),
    }


def publication_ready_sql(alias="github_actions"):
    """Use the same eligibility predicate before LIMIT and in scheduler probes."""
    return f"""{alias}.audit_status = 'accepted' AND {alias}.publish_status = 'not_published'
      AND NOT EXISTS (SELECT 1 FROM attempts process JOIN worker_results result
          ON result.attempt_id=process.attempt_id WHERE result.result_id={alias}.result_id
          AND json_type(process.metadata_json,'$.dispatch.pid')='integer'
          AND json_extract(process.metadata_json,'$.process_stopped_at') IS NULL)
      AND COALESCE(json_extract({alias}.metadata_json, '$.publish.retryable'), 1) != 0
      AND (json_extract({alias}.metadata_json, '$.publish.next_retry_at') IS NULL
           OR julianday(json_extract({alias}.metadata_json, '$.publish.next_retry_at')) <= julianday(?))
      AND NOT EXISTS (SELECT 1 FROM publication_intents pi WHERE pi.action_id={alias}.action_id AND pi.status='attention')
      AND NOT EXISTS (SELECT 1 FROM github_actions earlier WHERE earlier.result_id={alias}.result_id
          AND earlier.publish_status='not_published' AND earlier.audit_status IN ('pending','accepted')
          AND (earlier.audit_status='pending' OR json_extract(earlier.metadata_json, '$.publish.status')='publish_failed'
               OR EXISTS (SELECT 1 FROM publication_intents ep WHERE ep.action_id=earlier.action_id AND ep.status='attention'))
          AND (earlier.created_at < {alias}.created_at OR (earlier.created_at={alias}.created_at AND earlier.action_id < {alias}.action_id)))"""


def worker_error(logs):
    """Use stderr and terminal protocol errors, never quoted stdout content."""
    messages = []
    for kind, payload in logs.items():
        if not isinstance(payload, dict):
            continue
        tail = str(payload.get("tail", ""))
        if "stderr" in kind:
            messages.append(tail)
            continue
        for line in reversed(tail.splitlines()):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "turn.completed":
                break
            if event.get("type") in {"error", "turn.failed"}:
                error = event.get("error") or event.get("message") or ""
                messages.append(str(error.get("message", "")) if isinstance(error, dict) else str(error))
                if event.get("type") == "turn.failed":
                    break
    return "\n".join(messages)


def terminal_rate_limit(logs):
    """Recognize a worker's terminal rate limit, excluding quoted/recovered errors."""
    errors = []
    stderr = []
    for kind, payload in logs.items():
        tail = str(payload.get("tail", ""))
        if "stderr" in kind:
            stderr.append(tail)
            continue
        for line in reversed(tail.splitlines()):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "turn.completed":
                return None
            if event.get("type") == "result":
                if not event.get("is_error"):
                    return None
                errors.append({"type": "result", "message": str(event.get("errors") or event.get("result") or "")})
                break
            if event.get("type") in {"error", "turn.failed"}:
                error = event.get("error") or event.get("message") or ""
                message = str(error.get("message", "")) if isinstance(error, dict) else str(error)
                errors.append({"type": event["type"], "message": message})
                break
    # A terminal protocol error takes precedence over diagnostic stderr.
    message = "\n".join(error["message"] for error in errors) if errors else "\n".join(stderr)
    if error_class(message) != "rate_limited":
        return None
    return {"type": errors[0]["type"] if errors else "stderr", "message": message}
