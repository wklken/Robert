"""Bounded health measurements that persist deduplicated delay notices."""

from robert_agent.persistence.queries import tasks as db_read_tasks

from robert_agent.persistence import database as db_database
from robert_agent.persistence import errors as db_errors
from robert_agent.persistence.dialects import sqlite as db_sqlite

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import agent_runs as db_read_agent_runs
from robert_agent.persistence.queries import daemon_events as db_read_daemon_events
from robert_agent.persistence.queries import notifications as db_read_notifications
from robert_agent.persistence.queries import projections as db_read_projections
from robert_agent.persistence.stores import notifications as db_write_notifications

import json
import os
from pathlib import Path
from contextlib import closing


def _control_failure_snapshot(conn, limit):
    """Track separate control lanes so a healthy drain cannot hide a dead scan."""
    streaks, recovered = {}, set()
    for lane in ("scan", "local", "publish", "context_refresh"):
        events = db_read_daemon_events.find_for_control_failure_snapshot(conn, lane, limit)
        for identifier, status in events:
            if status != "failed":
                if not streaks.get(lane):
                    recovered.add(lane)
                break
            streaks.setdefault(lane, []).append(identifier)
    # Abandoned executors are reconciled in agent_runs, not necessarily in a
    # daemon event. Their successful successor ends that failure streak.
    runs = db_read_agent_runs.find_for_control_failure_snapshot(conn, limit)
    for identifier, status, encoded in runs:
        if status != "failed" or json.loads(encoded or "{}").get("status") != "executor_interrupted":
            if not streaks.get("executor"):
                recovered.add("executor")
            break
        streaks.setdefault("executor", []).append(identifier)
    return streaks, recovered


def _persist_control_failure_alerts(conn, now, streaks, recovered, *, supervision_owns_alerts=False):
    for lane in {"scan", "local", "publish", "context_refresh", "executor"}:
        failures = streaks.get(lane, [])
        existing = db_read_notifications.find_for_persist_control_failure_alerts(conn, lane)
        if not failures and lane in recovered:
            for (identifier,) in existing:
                db_write_notifications.update_for_persist_control_failure_alerts(conn, identifier)
        elif (
            len(failures) >= 3
            and not existing
            and not (supervision_owns_alerts and lane in {"scan", "local", "publish"})
        ):
            db_write_notifications.record_control_stop(
                conn,
                "notification-control-" + lane + "-" + failures[-1],
                now,
                json.dumps(
                    {
                        "control_lane": lane,
                        "failure_count": len(failures),
                        "reason_code": "control_plane_failed",
                        "required_action": "Inspect repeated control failures and restore GitHub event processing.",
                    }
                ),
            )


def collect(db_path, *, now, repo_ids, limit=100, supervision_owns_alerts=False):
    if not repo_ids:
        return {}
    with closing(db_database.connect(db_path, timeout=0.1)) as conn:
        streaks, recovered = _control_failure_snapshot(conn, limit)
        control_failure_streak = max((len(failures) for failures in streaks.values()), default=0)
        pages = db_sqlite.page_count(conn)
        free = db_sqlite.freelist_count(conn)
        page_size = db_sqlite.page_size(conn)
        delayed = db_read_tasks.collect_filtered(conn, repo_ids, now, now, limit)
        orphaned = db_records.first(db_read_projections.find_for_collect(conn, now, now, limit))[0]
        unsent = db_records.first(db_read_projections.find_for_collect_2(conn, limit))[0]
        failed_notifications = db_records.first(db_read_notifications.find_for_collect(conn))[0]
        oldest_notification = db_records.first(db_read_notifications.find_for_collect_2(conn, now))[0]
    alerts_persisted = True
    try:
        with closing(db_database.connect(db_path, timeout=0.1)) as conn, conn:
            _persist_control_failure_alerts(
                conn, now, streaks, recovered, supervision_owns_alerts=supervision_owns_alerts
            )
            for task_id, reason in [] if supervision_owns_alerts else delayed:
                db_write_notifications.insert_for_collect(
                    conn,
                    "notification-delay-" + task_id + "-" + reason,
                    task_id,
                    now,
                    json.dumps(
                        {
                            "reason_code": reason,
                            "required_action": "Inspect queue capacity, worker state and publication failure details.",
                        }
                    ),
                )
    except db_errors.OperationalError as exc:
        if str(exc) not in {"database is locked", "database table is locked", "database schema is locked"}:
            raise
        alerts_persisted = False
    wal = Path(str(db_path) + "-wal")
    return {
        "control_failure_streak": control_failure_streak,
        "alerts_persisted": alerts_persisted,
        "database_bytes": pages * page_size,
        "reusable_bytes": free * page_size,
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
        "delayed_tasks_sample": len(delayed),
        "stale_runs_sample": orphaned,
        "pending_notifications_sample": unsent,
        "sample_limit": limit,
        "delivery_scope": "database",
        "failed_notifications": failed_notifications,
        "oldest_notification_seconds": oldest_notification,
        "notification_delivery_configured": bool(os.environ.get("ROBERT_NOTIFICATION_COMMAND")),
    }
