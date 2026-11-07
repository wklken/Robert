"""SQLite state helpers for the Robert daemon."""

from robert_agent.persistence import database as db_database
from robert_agent.persistence.dialects import sqlite as db_sqlite

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import daemon_events as db_read_daemon_events
from robert_agent.persistence.queries import daemon_runs as db_read_daemon_runs
from robert_agent.persistence.queries import leases as db_read_leases
from robert_agent.persistence.stores import daemon_events as db_write_daemon_events
from robert_agent.persistence.stores import daemon_runs as db_write_daemon_runs
from robert_agent.persistence.stores import leases as db_write_leases

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4


def _id(prefix):
    return f"{prefix}-{uuid4().hex[:12]}"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value):
    if isinstance(value, datetime):
        return value
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _decode_json(value, default=None):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def connect(db_path, *, busy_timeout_ms=5000):
    conn = db_database.connect(Path(db_path).expanduser())
    db_sqlite.configure(conn, foreign_keys=True)
    db_sqlite.configure(conn, busy_timeout_ms=busy_timeout_ms)
    return conn


def start_daemon_run(conn, *, config_path, owner_id, now=None):
    now = now or utc_now()
    daemon_run_id = owner_id
    db_write_daemon_runs.insert_for_start_daemon_run(conn, daemon_run_id, owner_id, str(config_path), now)
    return {
        "daemon_run_id": daemon_run_id,
        "status": "running",
        "owner_id": owner_id,
        "config_path": str(config_path),
        "started_at": now,
    }


def finish_daemon_run(conn, daemon_run_id, status, *, summary=None, error=None, now=None):
    now = now or utc_now()
    db_write_daemon_runs.update_for_finish_daemon_run(
        conn,
        status,
        now,
        json.dumps(summary or {}, ensure_ascii=False, sort_keys=True),
        json.dumps(error, ensure_ascii=False, sort_keys=True) if error else None,
        daemon_run_id,
    )


def record_event(conn, daemon_run_id, event_type, status, metadata=None, now=None):
    now = now or utc_now()
    daemon_event_id = _id("daemon-event")
    db_write_daemon_events.record_event(
        conn,
        daemon_event_id,
        daemon_run_id,
        event_type,
        status,
        now,
        json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
    )
    return {
        "daemon_event_id": daemon_event_id,
        "daemon_run_id": daemon_run_id,
        "event_type": event_type,
        "status": status,
        "created_at": now,
        "metadata": metadata or {},
    }


def _row_payload(row):
    if not row:
        return None
    payload = dict(row)
    if "summary_json" in payload:
        payload["summary"] = _decode_json(payload.pop("summary_json"), {}) or {}
    if "error_json" in payload:
        payload["error"] = _decode_json(payload.pop("error_json"), None)
    if "metadata_json" in payload:
        payload["metadata"] = _decode_json(payload.pop("metadata_json"), {}) or {}
    return payload


def latest_daemon_summary(conn, event_limit=8):
    run = db_records.first(db_read_daemon_runs.find_for_latest_daemon_summary(conn))
    event = db_records.first(db_read_daemon_events.find_for_latest_daemon_summary(conn))
    lease = db_records.first(db_read_leases.find_for_latest_daemon_summary(conn))
    events = db_read_daemon_events.find_for_latest_daemon_summary_2(conn, event_limit)
    return {
        "available": True,
        "latest_run": _row_payload(run),
        "latest_event": _row_payload(event),
        "latest_lease": _row_payload(lease),
        "recent_events": [_row_payload(row) for row in events],
    }


def acquire_daemon_lease(conn, *, resource_key, owner_id, ttl_seconds, now=None):
    now = now or utc_now()
    now_dt = _parse_time(now)
    expires_at = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()
    db_write_leases.update_for_acquire_daemon_lease(conn, resource_key, now)
    active = db_records.first(db_read_leases.find_for_acquire_daemon_lease(conn, resource_key))
    if active:
        return {
            "ok": False,
            "status": "skipped_active_daemon",
            "lease_id": active["lease_id"],
            "owner_id": active["owner_id"],
            "expires_at": active["expires_at"],
        }
    lease_id = _id("lease")
    db_write_leases.insert_for_acquire_daemon_lease(conn, lease_id, resource_key, owner_id, now, expires_at, now)
    return {
        "ok": True,
        "status": "acquired",
        "lease_id": lease_id,
        "expires_at": expires_at,
    }


def heartbeat_daemon_lease(conn, lease_id, ttl_seconds, now=None):
    now = now or utc_now()
    expires_at = (_parse_time(now) + timedelta(seconds=ttl_seconds)).isoformat()
    db_write_leases.update_for_heartbeat_daemon_lease(conn, now, expires_at, lease_id)


def release_daemon_lease(conn, lease_id, now=None):
    now = now or utc_now()
    db_write_leases.update_for_release_daemon_lease(conn, now, now, lease_id)


def repo_id_for_full_name(full_name):
    return f"repo:{full_name}"


def run_exists(conn, daemon_run_id):
    return db_records.first(db_read_daemon_runs.find_for_run_exists(conn, daemon_run_id)) is not None


def started_at(conn, daemon_run_id):
    row = db_records.first(db_read_daemon_runs.find_started_at_by_daemon_run_id(conn, daemon_run_id))
    return row[0] if row else None
