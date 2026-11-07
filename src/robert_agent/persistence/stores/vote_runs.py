"""Shared stores operations for vote_runs."""

from robert_agent.foundation.contracts.execution import ModelRunStatus

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, bindparam, and_
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_for_new_run(
    conn,
    run_id,
    round_id,
    stage,
    worker_id,
    model,
    attempt_no,
    owner_attempt_id,
    value_8,
    prompt_sha256,
    timestamp,
    timestamp_11,
):
    stmt = insert(tables["vote_runs"]).values(
        {
            "run_id": bindparam("p0"),
            "round_id": bindparam("p1"),
            "stage": bindparam("p2"),
            "worker_id": bindparam("p3"),
            "model": bindparam("p4"),
            "attempt_no": bindparam("p5"),
            "owner_attempt_id": bindparam("p6"),
            "status": sql_constant(ModelRunStatus.PREPARED.value),
            "output_dir": bindparam("p7"),
            "prompt_sha256": bindparam("p8"),
            "created_at": bindparam("p9"),
            "updated_at": bindparam("p10"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": run_id,
            "p1": round_id,
            "p2": stage,
            "p3": worker_id,
            "p4": model,
            "p5": attempt_no,
            "p6": owner_attempt_id,
            "p7": value_8,
            "p8": prompt_sha256,
            "p9": timestamp,
            "p10": timestamp_11,
        },
    )


def update_for_release_slot(conn, run_id):
    stmt = (
        update(tables["vote_runs"])
        .values({"slot_reserved": sql_constant(0)})
        .where((tables["vote_runs"].c["run_id"] == bindparam("p0")))
    )
    return statements.write(conn, stmt, {"p0": run_id})


def update_for_release_finished_slot(conn, run_id, updated_at):
    stmt = (
        update(tables["vote_runs"])
        .values({"slot_reserved": sql_constant(0)})
        .where(
            and_(
                (tables["vote_runs"].c["run_id"] == bindparam("p0")),
                (tables["vote_runs"].c["updated_at"] == bindparam("p1")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": run_id, "p1": updated_at})


def update_for_initialize(conn):
    stmt = (
        update(tables["vote_runs"])
        .values({"slot_reserved": sql_constant(1)})
        .where(
            tables["vote_runs"]
            .c["status"]
            .in_([sql_constant(ModelRunStatus.STARTING.value), sql_constant(ModelRunStatus.RUNNING.value)])
        )
    )
    return statements.write(conn, stmt, {})


def update_for_initialize_2(conn):
    stmt = (
        update(tables["vote_runs"])
        .values({"started_at": tables["vote_runs"].c["created_at"]})
        .where(
            tables["vote_runs"]
            .c["status"]
            .in_([sql_constant(ModelRunStatus.STARTING.value), sql_constant(ModelRunStatus.RUNNING.value)])
        )
    )
    return statements.write(conn, stmt, {})
