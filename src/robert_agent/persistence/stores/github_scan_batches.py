"""Shared stores operations for github_scan_batches."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, delete, bindparam, not_, null
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def update_for_fail_batch(conn, count, attention, retry_at, batch_id):
    stmt = (
        update(tables["github_scan_batches"])
        .values(
            {
                "retry_count": bindparam("p0"),
                "attention_reason": bindparam("p1"),
                "retry_at": bindparam("p2"),
            }
        )
        .where((tables["github_scan_batches"].c["batch_id"] == bindparam("p3")))
    )
    return statements.write(conn, stmt, {"p0": count, "p1": attention, "p2": retry_at, "p3": batch_id})


def insert_for_save_batch(conn, identifier, repo_id, context, value_4, when, when_6, value_7):
    stmt = (
        insert(tables["github_scan_batches"])
        .values(
            {
                "batch_id": bindparam("p0"),
                "repo_id": bindparam("p1"),
                "context_digest": bindparam("p2"),
                "raw_events_json": bindparam("p3"),
                "created_at": bindparam("p4"),
                "retry_at": bindparam("p5"),
                "complete_sources_json": bindparam("p6"),
            }
        )
        .prefix_with("OR IGNORE", dialect="sqlite")
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": identifier,
            "p1": repo_id,
            "p2": context,
            "p3": value_4,
            "p4": when,
            "p5": when_6,
            "p6": value_7,
        },
    )


def update_for_retry_batch(conn, when, batch_id):
    stmt = (
        update(tables["github_scan_batches"])
        .values(
            {
                "retry_count": sql_constant(0),
                "attention_reason": null(),
                "retry_at": bindparam("p0"),
            }
        )
        .where((tables["github_scan_batches"].c["batch_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": when, "p1": batch_id})


def delete_for_finish_batch(conn):
    stmt = delete(tables["github_scan_batches"]).where(
        not_(tables["github_scan_batches"].c["completed_at"].is_(null()))
    )
    return statements.write(conn, stmt, {})


def set_complete_sources_json_by_batch_id(conn, value_1, identifier):
    stmt = (
        update(tables["github_scan_batches"])
        .values({"complete_sources_json": bindparam("p0")})
        .where((tables["github_scan_batches"].c["batch_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": identifier})


def set_completed_at_by_batch_id(conn, when, batch_id):
    stmt = (
        update(tables["github_scan_batches"])
        .values({"completed_at": bindparam("p0")})
        .where((tables["github_scan_batches"].c["batch_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": when, "p1": batch_id})
