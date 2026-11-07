"""Shared stores operations for leases."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, bindparam, and_
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def update_for_interrupt_run(conn, now, now_2, run_id):
    stmt = (
        update(tables["leases"])
        .values(
            {
                "status": sql_constant("released"),
                "released_at": bindparam("p0"),
                "heartbeat_at": bindparam("p1"),
            }
        )
        .where(
            and_(
                tables["leases"].c["resource_type"] == sql_constant("agent_run"),
                tables["leases"].c["owner_id"] == bindparam("p2"),
                tables["leases"].c["status"] == sql_constant("active"),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": now_2, "p2": run_id})


def update_for_acquire(conn, resource_key):
    stmt = (
        update(tables["leases"])
        .values({"heartbeat_at": tables["leases"].c["heartbeat_at"]})
        .where(
            and_(
                (tables["leases"].c["resource_type"] == sql_constant("agent_run")),
                (tables["leases"].c["resource_key"] == bindparam("p0")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": resource_key})


def insert_for_acquire(conn, lease_id, resource_key, run_id, run_now, expires_at, run_now_6):
    stmt = insert(tables["leases"]).values(
        {
            "lease_id": bindparam("p0"),
            "resource_type": sql_constant("agent_run"),
            "resource_key": bindparam("p1"),
            "owner_id": bindparam("p2"),
            "status": sql_constant("active"),
            "acquired_at": bindparam("p3"),
            "expires_at": bindparam("p4"),
            "heartbeat_at": bindparam("p5"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": lease_id,
            "p1": resource_key,
            "p2": run_id,
            "p3": run_now,
            "p4": expires_at,
            "p5": run_now_6,
        },
    )


def update_for_release(conn, run_now, run_now_2, lease_id):
    stmt = (
        update(tables["leases"])
        .values(
            {
                "status": sql_constant("released"),
                "released_at": bindparam("p0"),
                "heartbeat_at": bindparam("p1"),
            }
        )
        .where((tables["leases"].c["lease_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": run_now_2, "p2": lease_id})


def update_for_acquire_2(conn, value_1):
    stmt = (
        update(tables["leases"])
        .values({"status": sql_constant("expired")})
        .where((tables["leases"].c["lease_id"] == bindparam("p0")))
    )
    return statements.write(conn, stmt, {"p0": value_1})


def update_for_acquire_daemon_lease(conn, resource_key, now):
    stmt = (
        update(tables["leases"])
        .values({"status": sql_constant("expired")})
        .where(
            and_(
                tables["leases"].c["resource_type"] == sql_constant("daemon"),
                tables["leases"].c["resource_key"] == bindparam("p0"),
                tables["leases"].c["status"] == sql_constant("active"),
                tables["leases"].c["expires_at"] <= bindparam("p1"),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": resource_key, "p1": now})


def insert_for_acquire_daemon_lease(conn, lease_id, resource_key, owner_id, now, expires_at, now_6):
    stmt = insert(tables["leases"]).values(
        {
            "lease_id": bindparam("p0"),
            "resource_type": sql_constant("daemon"),
            "resource_key": bindparam("p1"),
            "owner_id": bindparam("p2"),
            "status": sql_constant("active"),
            "acquired_at": bindparam("p3"),
            "expires_at": bindparam("p4"),
            "heartbeat_at": bindparam("p5"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": lease_id,
            "p1": resource_key,
            "p2": owner_id,
            "p3": now,
            "p4": expires_at,
            "p5": now_6,
        },
    )


def update_for_heartbeat_daemon_lease(conn, now, expires_at, lease_id):
    stmt = (
        update(tables["leases"])
        .values({"heartbeat_at": bindparam("p0"), "expires_at": bindparam("p1")})
        .where(
            and_(
                (tables["leases"].c["lease_id"] == bindparam("p2")),
                (tables["leases"].c["status"] == sql_constant("active")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": expires_at, "p2": lease_id})


def update_for_release_daemon_lease(conn, now, now_2, lease_id):
    stmt = (
        update(tables["leases"])
        .values(
            {
                "status": sql_constant("released"),
                "released_at": bindparam("p0"),
                "heartbeat_at": bindparam("p1"),
            }
        )
        .where(
            and_(
                (tables["leases"].c["lease_id"] == bindparam("p2")),
                (tables["leases"].c["status"] == sql_constant("active")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": now_2, "p2": lease_id})
