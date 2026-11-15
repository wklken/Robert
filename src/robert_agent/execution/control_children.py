"""Persist the startup handshake and ownership of managed control children."""

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_control_children as db_read_github_control_children
from robert_agent.persistence.queries import leases as db_read_leases
from robert_agent.persistence.stores import github_control_children as db_write_github_control_children
import json
from pathlib import Path
import re


def owner_lease_active(conn, owner, *, resource_key=None):
    return bool(db_records.first(db_read_leases.find_for_owner_lease_active(conn, owner, resource_key=resource_key)))


def source_controller_owner(
    conn, *, child_id, lane, database_path, requested_owner=None, wrapper_pid=None, wrapper_group=None
):
    """Resolve source ownership only from a running child and its exact database lease."""
    current = child(conn, child_id)
    scope = f"database:{Path(database_path).resolve()}"
    if (
        current is None
        or current[2] != lane
        or current[4] != "running"
        or not db_records.first(db_read_leases.find_for_owner_lease_active(conn, current[1], resource_key=scope))
    ):
        return None
    if wrapper_pid is not None or wrapper_group is not None:
        try:
            identity = json.loads(current[3])
        except (TypeError, ValueError):
            return None
        if (
            not isinstance(identity, dict)
            or identity.get("pid") != wrapper_pid
            or identity.get("process_group_id") != wrapper_group
        ):
            return None
    controller = current[1]
    # One-shot controllers have fresh execution identities, but renew the same
    # source owner across invocations. Resident controllers keep their run ID.
    owner = f"source-cli:{scope}" if re.fullmatch(r"source-cli-[0-9a-f]{32}", controller) else controller
    return owner if requested_owner in (None, owner) else None


def list_children(conn):
    return db_read_github_control_children.list_children(conn)


def child(conn, identifier):
    return db_records.first(db_read_github_control_children.find_for_child(conn, identifier))


def state(conn, identifier):
    return db_records.first(db_read_github_control_children.find_status_and_identity_json_by_child_id(conn, identifier))


def record_starting(conn, identifier, owner, lane):
    db_write_github_control_children.record_starting(conn, identifier, owner, lane)


def claim_starting(conn, identifier, identity):
    return db_write_github_control_children.update_for_claim_starting(conn, json.dumps(identity), identifier)


def cancel_starting(conn, identifier):
    return db_write_github_control_children.update_for_cancel_starting(conn, identifier)


def mark_retired(conn, identifier):
    return db_write_github_control_children.mark_retired(conn, identifier)


def mark_stopped(conn, identifier):
    db_write_github_control_children.mark_stopped(conn, identifier)


def prune_stopped(conn, owner, *, keep):
    db_write_github_control_children.delete_for_prune_stopped(conn, owner, keep)


def previous_owner_children(conn, *, scope_key, current_owner):
    return db_read_github_control_children.find_for_previous_owner_children(conn, current_owner, scope_key)
