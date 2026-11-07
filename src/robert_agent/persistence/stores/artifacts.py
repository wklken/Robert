"""Shared stores operations for artifacts."""

from sqlalchemy import update, bindparam, and_
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def record_dispatch_artifacts(conn, value_1, task_id, attempt_id, artifact_type, value_5, bytes_size, run_now):
    stmt = insert(tables["artifacts"]).values(
        {
            "artifact_id": bindparam("p0"),
            "task_id": bindparam("p1"),
            "attempt_id": bindparam("p2"),
            "artifact_type": bindparam("p3"),
            "path": bindparam("p4"),
            "bytes": bindparam("p5"),
            "created_at": bindparam("p6"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": value_1,
            "p1": task_id,
            "p2": attempt_id,
            "p3": artifact_type,
            "p4": value_5,
            "p5": bytes_size,
            "p6": run_now,
        },
    )


def insert_for_register(conn, value_1, task_id, attempt_id, value_4, path, value_6, st_size, value_8):
    stmt = (
        insert(tables["artifacts"])
        .values(
            {
                "artifact_id": bindparam("p0"),
                "task_id": bindparam("p1"),
                "attempt_id": bindparam("p2"),
                "artifact_type": bindparam("p3"),
                "path": bindparam("p4"),
                "sha256": bindparam("p5"),
                "bytes": bindparam("p6"),
                "created_at": bindparam("p7"),
            }
        )
        .prefix_with("OR IGNORE", dialect="sqlite")
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": value_1,
            "p1": task_id,
            "p2": attempt_id,
            "p3": value_4,
            "p4": path,
            "p5": value_6,
            "p6": st_size,
            "p7": value_8,
        },
    )


def update_for_refresh_prepared_task_locked(conn, st_size, run_now, attempt_id, artifact_type, path):
    stmt = (
        update(tables["artifacts"])
        .values({"bytes": bindparam("p0"), "created_at": bindparam("p1")})
        .where(
            and_(
                tables["artifacts"].c["attempt_id"] == bindparam("p2"),
                tables["artifacts"].c["artifact_type"] == bindparam("p3"),
                tables["artifacts"].c["path"] == bindparam("p4"),
            )
        )
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": st_size,
            "p1": run_now,
            "p2": attempt_id,
            "p3": artifact_type,
            "p4": path,
        },
    )


def insert_for_synthesize(conn, value_1, task_id, attempt_id, value_4, value_5, value_6, st_size, value_8):
    stmt = (
        insert(tables["artifacts"])
        .values(
            {
                "artifact_id": bindparam("p0"),
                "task_id": bindparam("p1"),
                "attempt_id": bindparam("p2"),
                "artifact_type": bindparam("p3"),
                "path": bindparam("p4"),
                "sha256": bindparam("p5"),
                "bytes": bindparam("p6"),
                "created_at": bindparam("p7"),
            }
        )
        .prefix_with("OR REPLACE", dialect="sqlite")
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": value_1,
            "p1": task_id,
            "p2": attempt_id,
            "p3": value_4,
            "p4": value_5,
            "p5": value_6,
            "p6": st_size,
            "p7": value_8,
        },
    )
