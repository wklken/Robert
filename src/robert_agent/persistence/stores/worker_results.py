"""Shared stores operations for worker_results."""

from sqlalchemy import literal

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, bindparam, func
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_canary_fixture(conn, value_1, value_2, now, value_4):
    stmt = insert(tables["worker_results"]).values(
        {
            "result_id": sql_constant("result-production-canary"),
            "task_id": sql_constant("task-production-canary"),
            "attempt_id": bindparam("p0"),
            "output_type": sql_constant("comment_analysis"),
            "consumed_event_fingerprints_json": bindparam("p1"),
            "verification_json": sql_constant("[]"),
            "handoff": sql_constant("production write canary"),
            "created_at": bindparam("p2"),
            "metadata_json": bindparam("p3"),
        }
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": value_2, "p2": now, "p3": value_4})


def set_metadata_json_by_result_id(conn, value_1, result_id):
    stmt = (
        update(tables["worker_results"])
        .values({"metadata_json": bindparam("p0")})
        .where((tables["worker_results"].c["result_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": result_id})


def update_for_audit_completed_results(conn, value_1, result_id):
    stmt = (
        update(tables["worker_results"])
        .values(
            {
                "metadata_json": func.json_set(
                    tables["worker_results"].c["metadata_json"],
                    sql_constant("$.audit_observation"),
                    func.json(bindparam("p0")),
                )
            }
        )
        .where((tables["worker_results"].c["result_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": result_id})


def record_result_db(
    conn,
    result_id,
    task_id,
    attempt_id,
    output_type,
    value_5,
    value_6,
    value_7,
    result_created_at,
    value_9,
):
    stmt = insert(tables["worker_results"]).values(
        {
            "result_id": bindparam("p0"),
            "task_id": bindparam("p1"),
            "attempt_id": bindparam("p2"),
            "output_type": bindparam("p3"),
            "consumed_event_fingerprints_json": bindparam("p4"),
            "verification_json": bindparam("p5"),
            "handoff": bindparam("p6"),
            "created_at": bindparam("p7"),
            "metadata_json": bindparam("p8"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": result_id,
            "p1": task_id,
            "p2": attempt_id,
            "p3": output_type,
            "p4": value_5,
            "p5": value_6,
            "p6": value_7,
            "p7": result_created_at,
            "p8": value_9,
        },
    )


def update_for_execute_command(conn, audit_result_id):
    stmt = (
        update(tables["worker_results"])
        .values(
            {
                "metadata_json": func.json_remove(
                    tables["worker_results"].c["metadata_json"],
                    sql_constant("$.audit_observation"),
                )
            }
        )
        .where((tables["worker_results"].c["result_id"] == bindparam("p0")))
    )
    return statements.write(conn, stmt, {"p0": audit_result_id})


def insert_fixture(conn, rows):
    stmt = insert(tables["worker_results"]).values(
        {
            "result_id": bindparam("p0"),
            "task_id": bindparam("p1"),
            "attempt_id": bindparam("p2"),
            "output_type": bindparam("p3"),
            "consumed_event_fingerprints_json": literal('["comment:1"]'),
            "verification_json": literal("[]"),
            "handoff": literal("ready"),
            "created_at": bindparam("p4"),
            "metadata_json": func.json_object(literal("result_contract"), literal("compact-v1")),
        }
    )
    return statements.write_many(conn, stmt, rows)
