#!/usr/bin/env python3
import argparse
from datetime import datetime, timezone

from robert_agent.common import emit


def _parse_time(value):
    if isinstance(value, datetime):
        return value
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def classify_attempt(
    heartbeat_at,
    started_at,
    now=None,
    stale_after_minutes=20,
    hard_timeout_minutes=90,
):
    now = now or datetime.now(timezone.utc)
    heartbeat_at = _parse_time(heartbeat_at)
    started_at = _parse_time(started_at)
    heartbeat_age = (now - heartbeat_at).total_seconds() / 60
    runtime_age = (now - started_at).total_seconds() / 60

    if runtime_age >= hard_timeout_minutes or heartbeat_age >= hard_timeout_minutes:
        return {
            "ok": False,
            "status": "failed_timeout",
            "terminate": True,
            "heartbeat_age_minutes": heartbeat_age,
            "runtime_minutes": runtime_age,
        }
    if heartbeat_age >= stale_after_minutes:
        return {
            "ok": True,
            "status": "stale",
            "terminate": False,
            "heartbeat_age_minutes": heartbeat_age,
            "runtime_minutes": runtime_age,
        }
    return {
        "ok": True,
        "status": "running",
        "terminate": False,
        "heartbeat_age_minutes": heartbeat_age,
        "runtime_minutes": runtime_age,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--heartbeat-at", required=True)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--stale-after-minutes", type=int, default=20)
    parser.add_argument("--hard-timeout-minutes", type=int, default=90)
    args = parser.parse_args(argv)
    return emit(
        classify_attempt(
            heartbeat_at=args.heartbeat_at,
            started_at=args.started_at,
            stale_after_minutes=args.stale_after_minutes,
            hard_timeout_minutes=args.hard_timeout_minutes,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
