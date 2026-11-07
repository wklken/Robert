"""Persist the startup handshake and ownership of managed control children."""

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_control_children as db_read_github_control_children
from robert_agent.persistence.queries import leases as db_read_leases
from robert_agent.persistence.stores import github_control_children as db_write_github_control_children
import json


def owner_lease_active(conn, owner):
    return bool(db_records.first(db_read_leases.find_for_owner_lease_active(conn, owner)))


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
