"""SQLite state helpers for the Robert daemon."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
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


def connect(db_path):
    conn = sqlite3.connect(Path(db_path).expanduser())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def start_daemon_run(conn, *, config_path, owner_id, now=None):
    now = now or utc_now()
    daemon_run_id = owner_id
    conn.execute(
        """
        INSERT INTO daemon_runs(
          daemon_run_id, status, owner_id, config_path, started_at
        )
        VALUES (?, 'running', ?, ?, ?)
        """,
        (daemon_run_id, owner_id, str(config_path), now),
    )
    return {
        "daemon_run_id": daemon_run_id,
        "status": "running",
        "owner_id": owner_id,
        "config_path": str(config_path),
        "started_at": now,
    }


def finish_daemon_run(conn, daemon_run_id, status, *, summary=None, error=None, now=None):
    now = now or utc_now()
    conn.execute(
        """
        UPDATE daemon_runs
        SET status = ?,
            finished_at = ?,
            summary_json = ?,
            error_json = ?
        WHERE daemon_run_id = ?
        """,
        (
            status,
            now,
            json.dumps(summary or {}, ensure_ascii=False, sort_keys=True),
            json.dumps(error, ensure_ascii=False, sort_keys=True) if error else None,
            daemon_run_id,
        ),
    )


def record_event(conn, daemon_run_id, event_type, status, metadata=None, now=None):
    now = now or utc_now()
    daemon_event_id = _id("daemon-event")
    conn.execute(
        """
        INSERT INTO daemon_events(
          daemon_event_id, daemon_run_id, event_type, status, created_at, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            daemon_event_id,
            daemon_run_id,
            event_type,
            status,
            now,
            json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
        ),
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
    run = conn.execute(
        """
        SELECT daemon_run_id, status, owner_id, config_path, started_at, finished_at,
               summary_json, error_json
        FROM daemon_runs
        ORDER BY started_at DESC, daemon_run_id DESC
        LIMIT 1
        """
    ).fetchone()
    event = conn.execute(
        """
        SELECT daemon_event_id, daemon_run_id, event_type, status, created_at,
               metadata_json
        FROM daemon_events
        ORDER BY created_at DESC, daemon_event_id DESC
        LIMIT 1
        """
    ).fetchone()
    lease = conn.execute(
        """
        SELECT lease_id, resource_key, owner_id, status, acquired_at, expires_at,
               heartbeat_at, released_at, metadata_json
        FROM leases
        WHERE resource_type = 'daemon'
        ORDER BY CASE WHEN status = 'active' THEN 0 ELSE 1 END,
                 acquired_at DESC, lease_id DESC
        LIMIT 1
        """
    ).fetchone()
    events = conn.execute(
        """
        SELECT daemon_event_id, daemon_run_id, event_type, status, created_at,
               metadata_json
        FROM daemon_events
        ORDER BY created_at DESC, daemon_event_id DESC
        LIMIT ?
        """,
        (event_limit,),
    ).fetchall()
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
    conn.execute(
        """
        UPDATE leases
        SET status = 'expired'
        WHERE resource_type = 'daemon'
          AND resource_key = ?
          AND status = 'active'
          AND expires_at <= ?
        """,
        (resource_key, now),
    )
    active = conn.execute(
        """
        SELECT lease_id, owner_id, expires_at
        FROM leases
        WHERE resource_type = 'daemon'
          AND resource_key = ?
          AND status = 'active'
        """,
        (resource_key,),
    ).fetchone()
    if active:
        return {
            "ok": False,
            "status": "skipped_active_daemon",
            "lease_id": active["lease_id"],
            "owner_id": active["owner_id"],
            "expires_at": active["expires_at"],
        }
    lease_id = _id("lease")
    conn.execute(
        """
        INSERT INTO leases(
          lease_id, resource_type, resource_key, owner_id, status,
          acquired_at, expires_at, heartbeat_at
        )
        VALUES (?, 'daemon', ?, ?, 'active', ?, ?, ?)
        """,
        (lease_id, resource_key, owner_id, now, expires_at, now),
    )
    return {
        "ok": True,
        "status": "acquired",
        "lease_id": lease_id,
        "expires_at": expires_at,
    }


def heartbeat_daemon_lease(conn, lease_id, ttl_seconds, now=None):
    now = now or utc_now()
    expires_at = (_parse_time(now) + timedelta(seconds=ttl_seconds)).isoformat()
    conn.execute(
        """
        UPDATE leases
        SET heartbeat_at = ?,
            expires_at = ?
        WHERE lease_id = ?
          AND status = 'active'
        """,
        (now, expires_at, lease_id),
    )


def release_daemon_lease(conn, lease_id, now=None):
    now = now or utc_now()
    conn.execute(
        """
        UPDATE leases
        SET status = 'released',
            released_at = ?,
            heartbeat_at = ?
        WHERE lease_id = ?
          AND status = 'active'
        """,
        (now, now, lease_id),
    )


def cleanup_old_events(conn, retention_days, now=None):
    now = now or utc_now()
    cutoff = (_parse_time(now) - timedelta(days=retention_days)).isoformat()
    cur = conn.execute(
        """
        DELETE FROM daemon_events
        WHERE created_at < ?
        """,
        (cutoff,),
    )
    return cur.rowcount


def running_attempt_count(conn, repo_id):
    return conn.execute(
        """
        SELECT COUNT(*)
        FROM attempts a
        JOIN tasks t ON t.task_id = a.task_id
        JOIN workstreams w ON w.workstream_id = t.workstream_id
        WHERE w.repo_id = ?
          AND a.status IN ('running', 'stale')
          AND t.lifecycle IN ('queued', 'running')
        """,
        (repo_id,),
    ).fetchone()[0]


def running_attempt_count_for_repos(conn, repo_ids):
    repo_ids = [repo_id for repo_id in repo_ids if repo_id]
    if not repo_ids:
        return 0
    placeholders = ",".join("?" for _repo_id in repo_ids)
    return conn.execute(
        f"""
        SELECT COUNT(*)
        FROM attempts a
        JOIN tasks t ON t.task_id = a.task_id
        JOIN workstreams w ON w.workstream_id = t.workstream_id
        WHERE w.repo_id IN ({placeholders})
          AND a.status IN ('running', 'stale')
          AND t.lifecycle IN ('queued', 'running')
        """,
        repo_ids,
    ).fetchone()[0]


def capacity_full(conn, repo_id, max_concurrency):
    return running_attempt_count(conn, repo_id) >= max_concurrency


def capacity_full_for_repos(conn, repo_ids, max_concurrency):
    return running_attempt_count_for_repos(conn, repo_ids) >= max_concurrency


def repo_id_for_full_name(full_name):
    return f"repo:{full_name}"


def repo_id_for_config(config_result):
    repo_names = sorted(repo["full_name"] for repo in config_result.get("repos", []))
    return "config:" + ",".join(repo_names)
