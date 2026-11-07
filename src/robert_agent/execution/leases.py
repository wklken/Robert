"""Acquire repository run leases only after proving prior owner exit."""

from datetime import datetime, timedelta
from uuid import uuid4
from robert_agent.adapters.processes import identity as worker_identity
import json


def _json_object(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_time(value):
    if isinstance(value, datetime):
        return value
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def acquire(conn, repo_id, run_id, run_now, ttl_seconds):
    resource_key = repo_id
    now_dt = _parse_time(run_now)
    expires_at = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()
    # Acquire SQLite's writer lock before inspecting/inserting the unique lease.
    # Expiry is a scheduling hint, not proof that its process has stopped.
    conn.execute(
        "UPDATE leases SET heartbeat_at=heartbeat_at WHERE resource_type='agent_run' AND resource_key=?",
        (resource_key,),
    )
    active = conn.execute(
        """SELECT lease_id, owner_id, expires_at FROM leases
        WHERE resource_type='agent_run' AND resource_key=? AND status='active'""",
        (resource_key,),
    ).fetchone()
    if active and _parse_time(active[2]) <= now_dt:
        owner = conn.execute("SELECT summary_json FROM agent_runs WHERE run_id=?", (active[1],)).fetchone()
        identity = _json_object(owner[0]).get("executor_identity") if owner else None
        if identity and worker_identity.inspect_recorded_identity(identity) == "dead":
            conn.execute("UPDATE leases SET status='expired' WHERE lease_id=?", (active[0],))
            active = None
    if active:
        return {
            "ok": False,
            "status": "skipped_active_lease",
            "lease_id": active[0],
            "owner_id": active[1],
            "expires_at": active[2],
        }

    lease_id = f"lease-{uuid4().hex[:12]}"
    conn.execute(
        """
        INSERT INTO leases(
          lease_id, resource_type, resource_key, owner_id, status,
          acquired_at, expires_at, heartbeat_at
        )
        VALUES (?, 'agent_run', ?, ?, 'active', ?, ?, ?)
        """,
        (lease_id, resource_key, run_id, run_now, expires_at, run_now),
    )
    return {"ok": True, "status": "acquired", "lease_id": lease_id}


def release(conn, lease_id, run_now):
    if not lease_id:
        return
    conn.execute(
        """
        UPDATE leases
        SET status = 'released', released_at = ?, heartbeat_at = ?
        WHERE lease_id = ?
        """,
        (run_now, run_now, lease_id),
    )
