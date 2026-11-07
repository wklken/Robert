"""Shared stores operations for github_control_children."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, delete, bindparam, and_
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def record_starting(conn, identifier, owner, lane):
    stmt = insert(tables["github_control_children"]).values(
        {
            "child_id": bindparam("p0"),
            "owner_id": bindparam("p1"),
            "lane": bindparam("p2"),
            "identity_json": sql_constant("{}"),
            "status": sql_constant("starting"),
        }
    )
    return statements.write(conn, stmt, {"p0": identifier, "p1": owner, "p2": lane})


def mark_stopped(conn, identifier):
    stmt = (
        update(tables["github_control_children"])
        .values({"status": sql_constant("stopped")})
        .where((tables["github_control_children"].c["child_id"] == bindparam("p0")))
    )
    return statements.write(conn, stmt, {"p0": identifier})


def delete_for_prune_stopped(conn, owner, keep):
    stmt = delete(tables["github_control_children"]).where(
        and_(
            tables["github_control_children"].c["owner_id"] == bindparam("p0"),
            tables["github_control_children"].c["status"] == sql_constant("stopped"),
            tables["github_control_children"].c["child_id"] != bindparam("p1"),
        )
    )
    return statements.write(conn, stmt, {"p0": owner, "p1": keep})


def update_for_claim_starting(conn, value_1, identifier):
    stmt = (
        update(tables["github_control_children"])
        .values({"status": sql_constant("running"), "identity_json": bindparam("p0")})
        .where(
            and_(
                (tables["github_control_children"].c["child_id"] == bindparam("p1")),
                (tables["github_control_children"].c["status"] == sql_constant("starting")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": identifier})


def update_for_cancel_starting(conn, identifier):
    stmt = (
        update(tables["github_control_children"])
        .values({"status": sql_constant("stopped")})
        .where(
            and_(
                (tables["github_control_children"].c["child_id"] == bindparam("p0")),
                (tables["github_control_children"].c["status"] == sql_constant("starting")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": identifier})


def mark_retired(conn, identifier):
    stmt = (
        update(tables["github_control_children"])
        .values({"status": sql_constant("stopped")})
        .where(
            and_(
                (tables["github_control_children"].c["child_id"] == bindparam("p0")),
                tables["github_control_children"].c["status"].in_([sql_constant("starting"), sql_constant("running")]),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": identifier})
