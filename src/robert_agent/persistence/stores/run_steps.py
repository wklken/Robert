"""Shared stores operations for run_steps."""

from sqlalchemy import update, bindparam, and_, func
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_run_step(conn, value_1, run_id, key, status):
    stmt = insert(tables["run_steps"]).values(
        {
            "step_id": bindparam("p0"),
            "run_id": bindparam("p1"),
            "step_key": bindparam("p2"),
            "status": bindparam("p3"),
        }
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": run_id, "p2": key, "p3": status})


def mark_run_step(conn, status, run_now, finished_at, output_json, error_json, run_id, key):
    stmt = (
        update(tables["run_steps"])
        .values(
            {
                "status": bindparam("p0"),
                "started_at": func.coalesce(tables["run_steps"].c["started_at"], bindparam("p1")),
                "finished_at": bindparam("p2"),
                "output_json": func.coalesce(bindparam("p3"), tables["run_steps"].c["output_json"]),
                "error_json": func.coalesce(bindparam("p4"), tables["run_steps"].c["error_json"]),
            }
        )
        .where(
            and_(
                (tables["run_steps"].c["run_id"] == bindparam("p5")),
                (tables["run_steps"].c["step_key"] == bindparam("p6")),
            )
        )
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": status,
            "p1": run_now,
            "p2": finished_at,
            "p3": output_json,
            "p4": error_json,
            "p5": run_id,
            "p6": key,
        },
    )
