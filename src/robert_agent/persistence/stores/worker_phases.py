"""Shared stores operations for worker_phases."""

from sqlalchemy import bindparam
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_for_backfill_command_phases(conn, phase_id, attempt_id, phase, status, summary, value_6, finished_at):
    stmt = (
        insert(tables["worker_phases"])
        .values(
            {
                "phase_id": bindparam("p0"),
                "attempt_id": bindparam("p1"),
                "phase": bindparam("p2"),
                "status": bindparam("p3"),
                "summary": bindparam("p4"),
                "next_step": bindparam("p5"),
                "created_at": bindparam("p6"),
            }
        )
        .prefix_with("OR IGNORE", dialect="sqlite")
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": phase_id,
            "p1": attempt_id,
            "p2": phase,
            "p3": status,
            "p4": summary,
            "p5": value_6,
            "p6": finished_at,
        },
    )


def record_snapshot(conn, value_1, attempt_id, phase, status, summary, next_step, created_at):
    stmt = insert(tables["worker_phases"]).values(
        {
            "phase_id": bindparam("p0"),
            "attempt_id": bindparam("p1"),
            "phase": bindparam("p2"),
            "status": bindparam("p3"),
            "summary": bindparam("p4"),
            "next_step": bindparam("p5"),
            "created_at": bindparam("p6"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": value_1,
            "p1": attempt_id,
            "p2": phase,
            "p3": status,
            "p4": summary,
            "p5": next_step,
            "p6": created_at,
        },
    )
