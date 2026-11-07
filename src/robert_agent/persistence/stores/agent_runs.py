"""Shared stores operations for agent_runs."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, bindparam, and_
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_for_start_run(conn, run_id, started_at, value_3, value_4, summary_json):
    stmt = insert(tables["agent_runs"]).values(
        {
            "run_id": bindparam("p0"),
            "status": sql_constant("running"),
            "started_at": bindparam("p1"),
            "config_path": bindparam("p2"),
            "dry_run": bindparam("p3"),
            "summary_json": bindparam("p4"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": run_id,
            "p1": started_at,
            "p2": value_3,
            "p3": value_4,
            "p4": summary_json,
        },
    )


def update_for_interrupt_run(conn, now, error_json, run_id):
    stmt = (
        update(tables["agent_runs"])
        .values(
            {
                "status": sql_constant("failed"),
                "finished_at": bindparam("p0"),
                "error_json": bindparam("p1"),
            }
        )
        .where(
            and_(
                (tables["agent_runs"].c["run_id"] == bindparam("p2")),
                (tables["agent_runs"].c["status"] == sql_constant("running")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": error_json, "p2": run_id})


def update_for_finish_run(conn, status, finished_at, summary_json, run_id):
    stmt = (
        update(tables["agent_runs"])
        .values(
            {
                "status": bindparam("p0"),
                "finished_at": bindparam("p1"),
                "summary_json": bindparam("p2"),
            }
        )
        .where((tables["agent_runs"].c["run_id"] == bindparam("p3")))
    )
    return statements.write(conn, stmt, {"p0": status, "p1": finished_at, "p2": summary_json, "p3": run_id})


def update_for_finish_run_2(conn, status, finished_at, summary_json, value_4, run_id):
    stmt = (
        update(tables["agent_runs"])
        .values(
            {
                "status": bindparam("p0"),
                "finished_at": bindparam("p1"),
                "summary_json": bindparam("p2"),
                "error_json": bindparam("p3"),
            }
        )
        .where((tables["agent_runs"].c["run_id"] == bindparam("p4")))
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": status,
            "p1": finished_at,
            "p2": summary_json,
            "p3": value_4,
            "p4": run_id,
        },
    )
