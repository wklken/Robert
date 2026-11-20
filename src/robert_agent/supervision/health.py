"""supervision health responsibilities."""

from robert_agent.foundation.contracts.supervision import SupervisionMode

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from robert_agent.adapters.notifications import messaging as message_transport
from pathlib import Path
import sys
import tempfile
from robert_agent.configuration import supervision as supervision_config


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def runtime_dir(config):
    identity = hashlib.sha256(str(Path(config["db_path"]).resolve()).encode()).hexdigest()[:16]
    return Path(config["data_dir"]) / "supervision" / identity


def health_path(config):
    return runtime_dir(config) / "health.json"


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_health(config):
    try:
        return json.loads(health_path(config).read_text())
    except (OSError, ValueError):
        return {}


def health(config, result, now):
    previous = read_health(config)
    from robert_agent.supervision.rules import timestamp

    current = timestamp(now)
    poll = config.get("supervision", {}).get("poll_seconds", supervision_config.DEFAULTS["poll_seconds"])
    data = {
        "checked_at": now,
        "status": result["status"],
        "ok": result["ok"],
        "last_successful_check_at": now if result["ok"] else previous.get("last_successful_check_at"),
        "last_complete_check_at": (now if result.get("coverage_complete") else previous.get("last_complete_check_at")),
        "notification_delivery": "configured" if message_transport.configured(config) else "not_configured",
        "error_class": result.get("error_class"),
        "reason_code": result.get("reason_code"),
        "safe_error": result.get("safe_error"),
        "pid": os.getpid(),
    }
    if result.get("diagnostics"):
        data["diagnostics"] = result["diagnostics"]
    # A successful tick reports current health immediately, but does not reset
    # an outage's identity or delivery budget until recovery has been sampled.
    if previous.get("delivery_id"):
        for field in ("delivery_id", "first_failed_at", "delivery", "last_failure"):
            if field in previous:
                data[field] = previous[field]
    if not result["ok"]:
        data["first_failed_at"] = previous.get("first_failed_at", now)
        continuing = not previous.get("ok", True)
        data["consecutive_failures"] = previous.get("consecutive_failures", 1) + 1 if continuing else 1
        data["failure_streak_started_at"] = (
            previous.get("failure_streak_started_at", data["first_failed_at"]) if continuing else now
        )
        data["notification_eligible_at"] = (
            timestamp(data["failure_streak_started_at"]) + timedelta(seconds=poll)
        ).isoformat()
        data["delivery"] = previous.get("delivery", {})
        data["delivery_id"] = (
            "supervisor-health-"
            + hashlib.sha256((str(Path(config["db_path"]).resolve()) + data["first_failed_at"]).encode()).hexdigest()[
                :24
            ]
        )
        data["last_failure"] = {
            "checked_at": now,
            "reason_code": result.get("reason_code"),
            "error_class": result.get("error_class"),
            "safe_error": result.get("safe_error"),
            "diagnostics": result.get("diagnostics", {}),
        }
    elif data.get("delivery_id"):
        checked = timestamp(previous.get("checked_at"))
        consecutive = bool(
            previous.get("ok") and current and checked and 0 <= (current - checked).total_seconds() <= poll * 2 + 10
        )
        recovered_since = timestamp(previous.get("recovery_started_at")) if consecutive else current
        data["recovery_started_at"] = (recovered_since or current).isoformat()
        if consecutive and recovered_since and (current - recovered_since).total_seconds() >= max(300, poll * 2):
            for field in ("delivery_id", "first_failed_at", "delivery", "recovery_started_at"):
                data.pop(field, None)
    atomic_json(health_path(config), data)
    if not result["ok"] and not previous.get("delivery_id"):
        print(
            "Robert supervision: observation unavailable; health recorded independently of the database",
            file=sys.stderr,
        )


def owns_health_alerts(config, now):
    if not config or config.get("supervision", {}).get("mode") == SupervisionMode.OFF.value:
        return False
    health = read_health(config)
    from robert_agent.supervision.rules import timestamp

    current = timestamp(now)
    checked = timestamp(health.get("checked_at"))
    complete = timestamp(health.get("last_complete_check_at"))
    poll = config.get("supervision", {}).get("poll_seconds", 60)
    return bool(
        health.get("ok")
        and current
        and checked
        and complete
        and 0 <= (current - checked).total_seconds() <= poll * 2 + 10
        and 0 <= (current - complete).total_seconds() <= max(300, poll * 10)
    )


def runtime_status(config):
    try:
        path = runtime_dir(config) / "daemon.json"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as stream:
            import stat

            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_uid not in {0, os.geteuid()}:
                return None
            value = json.loads(stream.read(32768))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None
