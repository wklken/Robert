"""Budget limits shared by ordinary episodes and native repair cycles."""

from datetime import datetime, timezone
from math import isfinite

from robert_agent.foundation.timestamps import utc_timestamp


def _legacy_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def status(*, attempt_count, started_at, policy, usage, now, native=False, retry=False):
    result = {"ok": True, "reason": "", **usage}
    if not retry and attempt_count >= policy["max_attempts"]:
        return {**result, "ok": False, "reason": "max_attempts"}
    parse = utc_timestamp if native else _legacy_time
    start, current = parse(started_at), parse(now)
    if native and (current is None or (started_at is not None and (start is None or start > current))):
        return {**result, "ok": False, "reason": "invalid_clock"}
    if start is not None and current is not None:
        elapsed = (current - start).total_seconds() / 60
        result["elapsed_minutes"] = elapsed
        if elapsed >= policy["max_wall_minutes"]:
            return {**result, "ok": False, "reason": "max_wall_minutes"}
    expected = max(attempt_count, result["attempt_usage_count"])
    if (policy.get("max_total_tokens") and result["measured_token_attempt_count"] < expected) or (
        policy.get("max_cost_usd") and result["measured_cost_attempt_count"] < expected
    ):
        return {**result, "ok": False, "reason": "usage_unavailable"}
    for limit, value in (("max_total_tokens", "total_tokens"), ("max_cost_usd", "total_cost_usd")):
        if policy.get(limit) and result[value] >= policy[limit]:
            return {**result, "ok": False, "reason": limit}
    return result


def effective_native_policy(snapshot, current):
    """Active native cycles may shrink; mutable config cannot raise their limits."""
    fields = {"ci": ("max_wall_minutes", "max_total_tokens", "max_cost_usd"), "conflict": ("max_wall_minutes",)}
    result = {"max_attempts": 3}
    for kind, limits in fields.items():
        old, latest = snapshot[kind], current[kind]
        if not isinstance(old["enabled"], bool) or not isinstance(latest["enabled"], bool):
            raise ValueError("Invalid repair policy gate")
        values = {}
        for field in limits:
            bounds = (old[field], latest[field])
            if any(
                type(value) not in (int, float) or value <= 0 or isinstance(value, float) and not isfinite(value)
                for value in bounds
            ):
                raise ValueError("Invalid repair policy limit")
            values[field] = min(bounds)
        result[kind] = {"enabled": old["enabled"] and latest["enabled"], **values}
    for policy in (snapshot, current):
        values = policy["ci"]["check_allowlist"]
        if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("Invalid repair CI checks")
    current_checks = set(current["ci"]["check_allowlist"])
    result["ci"]["check_allowlist"] = list(
        dict.fromkeys(check for check in snapshot["ci"]["check_allowlist"] if check in current_checks)
    )
    return result


def native_usage(measurements):
    """Sum known values while preserving absence for every recorded attempt."""
    return {
        "attempt_usage_count": len(measurements),
        "measured_token_attempt_count": sum(row["tokens"] is not None for row in measurements),
        "measured_cost_attempt_count": sum(row["cost_usd"] is not None for row in measurements),
        "total_tokens": sum(row["tokens"] or 0 for row in measurements),
        "total_cost_usd": sum(row["cost_usd"] or 0 for row in measurements),
    }


def health_status(observation, *, authorized_base_ref, required_checks):
    """Read structured PR evidence. Success requires exact current-head checks."""
    if observation.state == "closed" or observation.merged is True:
        return "remote_pr_terminal"
    if observation.state != "open" or observation.merged is not False:
        return "health_unavailable"
    if observation.base_ref != authorized_base_ref:
        return "base_ref_changed"
    if observation.mergeable is not True or observation.checks_complete is not True or not required_checks:
        return "repair_pending"
    by_name = {name: [] for name in required_checks}
    for check in observation.checks:
        if check.name in by_name and check.head_sha == observation.head_sha:
            by_name[check.name].append(check)
    if all(
        len(rows) == 1 and rows[0].status == "completed" and rows[0].conclusion == "success"
        for rows in by_name.values()
    ):
        return "health_recovered"
    return "repair_pending"


def repair_signal(observation, *, mode, allowed_checks):
    if mode == "conflict":
        return observation.mergeable is False
    if observation.checks_complete is not True:
        return False
    checks = [row for row in observation.checks if row.head_sha == observation.head_sha and row.name in allowed_checks]
    return len({row.name for row in checks}) == len(checks) and any(
        row.status == "completed" and row.conclusion == "failure" for row in checks
    )
