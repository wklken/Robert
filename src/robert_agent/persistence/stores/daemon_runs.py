"""Shared stores operations for daemon_runs."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, bindparam
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_for_start_daemon_run(conn, daemon_run_id, owner_id, value_3, now):
    stmt = insert(tables["daemon_runs"]).values(
        {
            "daemon_run_id": bindparam("p0"),
            "status": sql_constant("running"),
            "owner_id": bindparam("p1"),
            "config_path": bindparam("p2"),
            "started_at": bindparam("p3"),
        }
    )
    return statements.write(conn, stmt, {"p0": daemon_run_id, "p1": owner_id, "p2": value_3, "p3": now})


def update_for_finish_daemon_run(conn, status, now, value_3, value_4, daemon_run_id):
    stmt = (
        update(tables["daemon_runs"])
        .values(
            {
                "status": bindparam("p0"),
                "finished_at": bindparam("p1"),
                "summary_json": bindparam("p2"),
                "error_json": bindparam("p3"),
            }
        )
        .where((tables["daemon_runs"].c["daemon_run_id"] == bindparam("p4")))
    )
    return statements.write(
        conn,
        stmt,
        {"p0": status, "p1": now, "p2": value_3, "p3": value_4, "p4": daemon_run_id},
    )
