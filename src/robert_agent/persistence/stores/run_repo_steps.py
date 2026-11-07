"""Shared stores operations for run_repo_steps."""

from sqlalchemy import update, bindparam, and_, func
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_repo_run_step(conn, value_1, run_id, repo_id, key, status):
    stmt = (
        insert(tables["run_repo_steps"])
        .values(
            {
                "step_id": bindparam("p0"),
                "run_id": bindparam("p1"),
                "repo_id": bindparam("p2"),
                "step_key": bindparam("p3"),
                "status": bindparam("p4"),
            }
        )
        .prefix_with("OR IGNORE", dialect="sqlite")
    )
    return statements.write(
        conn,
        stmt,
        {"p0": value_1, "p1": run_id, "p2": repo_id, "p3": key, "p4": status},
    )


def mark_repo_run_step(conn, status, run_now, finished_at, output_json, error_json, run_id, repo_id, key):
    stmt = (
        update(tables["run_repo_steps"])
        .values(
            {
                "status": bindparam("p0"),
                "started_at": func.coalesce(tables["run_repo_steps"].c["started_at"], bindparam("p1")),
                "finished_at": func.coalesce(bindparam("p2"), tables["run_repo_steps"].c["finished_at"]),
                "output_json": func.coalesce(bindparam("p3"), tables["run_repo_steps"].c["output_json"]),
                "error_json": func.coalesce(bindparam("p4"), tables["run_repo_steps"].c["error_json"]),
            }
        )
        .where(
            and_(
                tables["run_repo_steps"].c["run_id"] == bindparam("p5"),
                tables["run_repo_steps"].c["repo_id"] == bindparam("p6"),
                tables["run_repo_steps"].c["step_key"] == bindparam("p7"),
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
            "p6": repo_id,
            "p7": key,
        },
    )
