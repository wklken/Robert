"""Acquire repository run leases only after proving prior owner exit."""

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import agent_runs as db_read_agent_runs
from robert_agent.persistence.queries import leases as db_read_leases
from robert_agent.persistence.stores import leases as db_write_leases
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


def observe_owner(conn, repo_id, run_now):
    """Observe an expired owner's identity before acquiring a database writer."""
    if conn.in_transaction:
        raise ValueError("lease owner observation requires no active transaction")
    active = db_records.first(db_read_leases.find_for_acquire(conn, repo_id))
    if not active or _parse_time(active[2]) > _parse_time(run_now):
        return None
    owner = db_records.first(db_read_agent_runs.find_summary_json_by_run_id(conn, active[1]))
    identity = _json_object(owner[0]).get("executor_identity") if owner else None
    if not identity:
        return None
    return {"lease": tuple(active), "identity": identity, "status": worker_identity.inspect_recorded_identity(identity)}


def acquire(conn, repo_id, run_id, run_now, ttl_seconds, *, observation=None):
    resource_key = repo_id
    now_dt = _parse_time(run_now)
    expires_at = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()
    if observation is None and not conn.in_transaction:
        observation = observe_owner(conn, repo_id, run_now)
    # Serialize only the durable transition; recheck the observed owner below.
    # A caller already holding a transaction must supply an earlier observation.
    db_write_leases.update_for_acquire(conn, resource_key)
    active = db_records.first(db_read_leases.find_for_acquire(conn, resource_key))
    if active and _parse_time(active[2]) <= now_dt:
        owner = db_records.first(db_read_agent_runs.find_summary_json_by_run_id(conn, active[1]))
        identity = _json_object(owner[0]).get("executor_identity") if owner else None
        if (
            observation
            and observation["lease"] == tuple(active)
            and observation["identity"] == identity
            and observation["status"] == "dead"
        ):
            db_write_leases.update_for_acquire_2(conn, active[0])
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
    db_write_leases.insert_for_acquire(conn, lease_id, resource_key, run_id, run_now, expires_at, run_now)
    return {"ok": True, "status": "acquired", "lease_id": lease_id}


def release(conn, lease_id, run_now):
    if not lease_id:
        return
    db_write_leases.update_for_release(conn, run_now, run_now, lease_id)
