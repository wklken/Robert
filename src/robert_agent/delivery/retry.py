"""Pure publication retry scheduling and automatic retry eligibility."""

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import math
import re

from robert_agent.execution import retry as execution_retry
from robert_agent.foundation.timestamps import utc_timestamp


MAX_ATTEMPTS = execution_retry.MAX_ATTEMPTS
MAX_ELAPSED_SECONDS = execution_retry.MAX_ELAPSED_SECONDS
RESET_MARGIN_SECONDS = 1
_ERROR_CLASSES = frozenset(
    {
        "transient_failure",
        "network_failure",
        "rate_limited",
        "authentication_required",
        "permission_required",
        "branch_conflict",
        "configuration_required",
    }
)


def _prior_facts(prior, current):
    """Validate stored facts without coercing corrupt counts or restarting a budget."""
    if not isinstance(prior, dict) or current is None:
        return None
    if "error_class" in prior and (
        not isinstance(prior["error_class"], str) or prior["error_class"] not in _ERROR_CLASSES
    ):
        return None
    count = prior.get("attempt_count", 0)
    if type(count) is not int or not 0 <= count <= MAX_ATTEMPTS:
        return None
    if "max_attempts" in prior and (type(prior["max_attempts"]) is not int or prior["max_attempts"] != MAX_ATTEMPTS):
        return None
    for key in ("retryable", "attempt_charged", "reconcile_required"):
        if key in prior and type(prior[key]) is not bool:
            return None
    first = utc_timestamp(prior.get("first_failed_at"))
    if first is None:
        if count or "first_failed_at" in prior:
            return None
        first = current
    if first > current:
        return None
    dates = {}
    for key in ("failed_at", "next_retry_at", "retry_not_before", "last_attempt_at"):
        value = prior.get(key)
        if value is None:
            if key in {"failed_at", "last_attempt_at"} and key in prior:
                return None
            dates[key] = None
            continue
        parsed = utc_timestamp(value)
        if parsed is None or parsed < first:
            return None
        if key in {"failed_at", "last_attempt_at"} and parsed > current:
            return None
        dates[key] = parsed
    failed = dates["failed_at"] or first
    if dates["next_retry_at"] is not None and dates["next_retry_at"] < failed:
        return None
    floor = dates["retry_not_before"]
    wait = prior.get("provider_wait_seconds", 0)
    if type(wait) not in (int, float) or not 0 <= wait <= 253402300799 or not math.isfinite(wait):
        return None
    if "provider_wait_seconds" not in prior and floor is not None:
        # Compatibility with already persisted delivery retry metadata.
        wait = max(0, (floor - (failed if prior.get("error_class") == "rate_limited" else first)).total_seconds())
    return count, first, dates["next_retry_at"], floor, wait


def _policy_facts(prior, current):
    facts = _prior_facts(prior, current)
    if facts is None:
        return None
    nested = None
    if "reconcile_retry" in prior:
        read = prior["reconcile_retry"]
        if not isinstance(read, dict) or "reconcile_retry" in read:
            return None
        nested = _prior_facts(read, current)
        if nested is None or nested[1] < facts[1]:
            return None
        if read.get("reconcile_provider_wait_seconds", 0) != 0:
            return None
        if nested[4] > ((nested[3] - nested[1]).total_seconds() if nested[3] is not None else 0):
            return None
    accounted = prior.get("reconcile_provider_wait_seconds", 0)
    if type(accounted) not in (int, float) or not 0 <= accounted <= facts[4] or not math.isfinite(accounted):
        return None
    if accounted and (nested is None or accounted > nested[4]):
        return None
    own_wait = facts[4] - accounted
    if own_wait > ((facts[3] - facts[1]).total_seconds() if facts[3] is not None else 0):
        return None
    facts = (*facts[:4], own_wait)
    return facts, nested


def _wait_totals(facts, nested):
    _, first, _, floor, wait = facts
    if nested is not None:
        _, read_first, _, read_floor, read_wait = nested
        # The counters belong to separate owners. Count overlapping forced waits
        # once; the nested record stays separate across write failure recording.
        overlap = max(0, (floor - read_first).total_seconds()) if floor is not None else 0
        overlap = min(overlap, read_wait, wait)
        return wait + read_wait - overlap, read_wait - overlap
    return wait, 0


def _elapsed(facts, nested, current):
    return max(0, (current - facts[1]).total_seconds() - _wait_totals(facts, nested)[0])


def _header_values(headers, message):
    """Read only scheduling hints; other headers and diagnostic text are never returned."""
    values = {"retry-after": [], "x-ratelimit-remaining": [], "x-ratelimit-reset": []}
    if isinstance(headers, Mapping):
        for name, value in headers.items():
            if isinstance(name, str) and name.lower() in values:
                values[name.lower()].append(value)
    if isinstance(message, str):
        for name, value in re.findall(
            r"(?im)^\s*(retry-after|x-ratelimit-remaining|x-ratelimit-reset)\s*:[ \t]*([^\r\n]*)", message
        ):
            values[name.lower()].append(value)
    return values


def _seconds(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _retry_after(value, current):
    seconds = _seconds(value)
    if seconds is not None:
        # Ignore unrepresentable numbers rather than overflowing or clamping a provider deadline.
        if seconds > Decimal(253402300799):
            return None
        try:
            return current + timedelta(seconds=float(seconds))
        except OverflowError:
            return None
    if not isinstance(value, str) or len(value) > 128 or "\r" in value or "\n" in value:
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _provider_deadline(headers, current):
    hints = [date for value in headers["retry-after"] if (date := _retry_after(value, current)) is not None]
    exhausted = any(_seconds(value) == 0 for value in headers["x-ratelimit-remaining"])
    if exhausted:
        for value in headers["x-ratelimit-reset"]:
            seconds = _seconds(value)
            if seconds is None or seconds > Decimal(253402300799):
                continue
            try:
                hints.append(
                    datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=float(seconds) + RESET_MARGIN_SECONDS)
                )
            except OverflowError:
                continue
    return max(hints) if hints else None, exhausted


def _category(message, explicit, status, deadline, exhausted):
    if type(status) is int and status == 401:
        return "authentication_required"
    inferred = execution_retry.error_class(message)
    if type(status) is int and status == 429:
        return "rate_limited"
    if type(status) is int and status == 403:
        if explicit == "rate_limited" or inferred == "rate_limited" or exhausted or deadline is not None:
            return "rate_limited"
        return "permission_required"
    return explicit if isinstance(explicit, str) and explicit in _ERROR_CLASSES else inferred


def _scheduled_rate_limit(prior, facts):
    count, _, due, floor, _ = facts
    return (
        0 < count < MAX_ATTEMPTS
        and prior.get("error_class") == "rate_limited"
        and prior.get("retryable") is True
        and due is not None
        and floor is not None
    )


def _disable(state):
    state.update(
        retryable=False,
        next_retry_at=None,
        reason_code="retry_budget_exhausted",
        required_action="Inspect the failure and explicitly retry after resolving its cause.",
    )
    return state


def failure_state(previous, message, now, *, retryable=True, error_class=None, response_headers=None, http_status=None):
    """Charge one failure and schedule its next attempt without waiting or retaining diagnostics.

    Callers supply proven transport classification and keep reconciliation read
    metadata separate from the actual publication write budget. Persist the
    returned provider_wait_seconds and reconcile_provider_wait_seconds alongside
    the full reconcile_retry record; the latter counter prevents counting the
    retained read wait again on subsequent write failures.
    """
    current = utc_timestamp(now)
    policy = _policy_facts(previous, current)
    if policy is None:
        fallback = now if current is not None else "1970-01-01T00:00:00+00:00"
        state = execution_retry.failure_state(
            {"attempt_count": MAX_ATTEMPTS - 1, "first_failed_at": fallback}, "", fallback, retryable=False
        )
        return _disable(state)

    facts, nested = policy
    count, first, _, prior_floor, wait = facts
    headers = _header_values(response_headers, message)
    provider, exhausted = _provider_deadline(headers, current)
    category = _category(message, error_class, http_status, provider, exhausted)
    # Reuse the shared field contract and permanent/generic behavior unchanged.
    shared_message = {
        "rate_limited": "HTTP 429",
        "network_failure": "",
        "authentication_required": "HTTP 401 Unauthorized",
        "permission_required": "HTTP 403 Forbidden",
        "branch_conflict": "non-fast-forward",
        "configuration_required": "invalid configuration",
    }.get(category, message)
    try:
        state = execution_retry.failure_state(previous, shared_message, now, retryable=retryable is True)
        base_due = current + timedelta(seconds=5 * 2 ** min(count, 4))
    except OverflowError:
        state = execution_retry.failure_state(previous, shared_message, now, retryable=False)
        state["error_class"] = category
        if prior_floor is not None:
            state["retry_not_before"] = prior_floor.isoformat()
        return _disable(state)
    state["error_class"] = category
    if nested is not None:
        read = previous["reconcile_retry"]
        keys = set(state) | {"retry_not_before", "provider_wait_seconds"}
        state["reconcile_retry"] = {key: value for key, value in read.items() if key in keys}
        if "reconcile_required" in previous:
            state["reconcile_required"] = previous["reconcile_required"]
    floor = prior_floor
    if category == "rate_limited":
        if provider is None or provider <= current:
            try:
                provider = current + timedelta(seconds=60 * 2 ** min(count, 4))
            except OverflowError:
                return _disable(state)
        floor = max(provider, prior_floor) if prior_floor is not None else provider
        wait += max(0, (floor - max(current, prior_floor or current)).total_seconds())
        due = max(base_due, floor)
        allowed = retryable is True and count + 1 < MAX_ATTEMPTS
    elif category == "network_failure":
        due = max(base_due, floor) if floor is not None else base_due
        if nested is not None and nested[3] is not None:
            due = max(due, nested[3])
        allowed = retryable is True and count + 1 < MAX_ATTEMPTS and _elapsed(facts, nested, due) < MAX_ELAPSED_SECONDS
    else:
        if floor is None:
            return state
        due = utc_timestamp(state["next_retry_at"])
        if due is not None and floor is not None:
            due = max(due, floor)
        allowed = state["retryable"]

    if floor is not None:
        state["retry_not_before"] = floor.isoformat()
    if floor is not None or nested is not None or "provider_wait_seconds" in previous:
        total, accounted = _wait_totals((*facts[:3], floor, wait), nested)
        state["provider_wait_seconds"] = total
        if nested is not None:
            state["reconcile_provider_wait_seconds"] = accounted
    if category in {"network_failure", "rate_limited"}:
        if not allowed:
            return _disable(state)
        state.update(retryable=True, reason_code="retry_scheduled", required_action=None)
    state["next_retry_at"] = due.isoformat() if allowed and due is not None else None
    return state


def automatic_retry(prior, now):
    """Allow a due retry only when stored failure facts prove automatic eligibility."""
    current = utc_timestamp(now)
    policy = _policy_facts(prior, current)
    if policy is None or prior.get("retryable") is not True:
        return False
    facts, nested = policy
    count, first, due, floor, _ = facts
    if not 0 < count < MAX_ATTEMPTS or due is None:
        return False
    if prior.get("error_class") not in {"network_failure", "rate_limited"}:
        return False
    boundary = max(due, floor) if floor is not None else due
    if nested is not None:
        read = prior["reconcile_retry"]
        if prior.get("reconcile_required") is True and not automatic_retry(read, now):
            return False
        if nested[3] is not None:
            boundary = max(boundary, nested[3])
    if boundary > current or retry_budget_exhausted(prior, now):
        return False
    return _scheduled_rate_limit(prior, facts) or _elapsed(facts, nested, boundary) < MAX_ELAPSED_SECONDS


def retry_budget_exhausted(prior, now):
    """Bound writes by attempts; scheduled provider waits do not spend the elapsed budget."""
    current = utc_timestamp(now)
    policy = _policy_facts(prior, current)
    if policy is None:
        return True
    facts, nested = policy
    count = facts[0]
    if count >= MAX_ATTEMPTS:
        return True
    if _scheduled_rate_limit(prior, facts):
        return False
    if prior.get("error_class") == "network_failure":
        return _elapsed(facts, nested, current) >= MAX_ELAPSED_SECONDS
    return (current - facts[1]).total_seconds() >= MAX_ELAPSED_SECONDS
