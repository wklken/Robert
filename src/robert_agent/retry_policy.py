"""Bounded retry decisions shared by publication and durable delivery."""

from datetime import datetime, timedelta, timezone
import re

MAX_ATTEMPTS = 3
MAX_ELAPSED_SECONDS = 600


def error_class(message):
    text = str(message or "").lower()
    if re.search(
        r"\b401\b|unauthorized|auth expired|authentication failed|invalid (?:api )?key|not logged in",
        text,
    ):
        return "authentication_required"
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
        for value in ("missing fields", "not a valid", "invalid configuration")
    ):
        return "configuration_required"
    return "transient_failure"


def timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def failure_state(previous, message, now, *, retryable=True):
    count = int(previous.get("attempt_count", 0)) + 1
    first = previous.get("first_failed_at") or now
    category = error_class(message)
    elapsed = (timestamp(now) - timestamp(first)).total_seconds()
    allowed = (
        retryable
        and category == "transient_failure"
        and count < MAX_ATTEMPTS
        and elapsed < MAX_ELAPSED_SECONDS
    )
    reason = (
        category
        if category != "transient_failure"
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
            timestamp(now) + timedelta(seconds=min(60, 5 * 2 ** min(count - 1, 4)))
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
      AND COALESCE(json_extract({alias}.metadata_json, '$.publish.retryable'), 1) != 0
      AND (json_extract({alias}.metadata_json, '$.publish.next_retry_at') IS NULL
           OR julianday(json_extract({alias}.metadata_json, '$.publish.next_retry_at')) <= julianday(?))
      AND NOT EXISTS (SELECT 1 FROM publication_intents pi WHERE pi.action_id={alias}.action_id AND pi.status='attention')
      AND NOT EXISTS (SELECT 1 FROM github_actions earlier WHERE earlier.result_id={alias}.result_id
          AND earlier.publish_status='not_published' AND earlier.audit_status IN ('pending','accepted')
          AND (earlier.audit_status='pending' OR json_extract(earlier.metadata_json, '$.publish.status')='publish_failed'
               OR EXISTS (SELECT 1 FROM publication_intents ep WHERE ep.action_id=earlier.action_id AND ep.status='attention'))
          AND (earlier.created_at < {alias}.created_at OR (earlier.created_at={alias}.created_at AND earlier.action_id < {alias}.action_id)))"""
