"""Shared stores operations for attempts."""

from sqlalchemy import literal

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import (
    select,
    update,
    bindparam,
    and_,
    null,
    case,
    func,
    LABEL_STYLE_NONE,
)
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_canary_fixture(conn, value_1, now, now_3, now_4):
    stmt = insert(tables["attempts"]).values(
        {
            "attempt_id": bindparam("p0"),
            "task_id": sql_constant("task-production-canary"),
            "attempt_no": sql_constant(1),
            "status": sql_constant("completed"),
            "started_at": bindparam("p1"),
            "heartbeat_at": bindparam("p2"),
            "finished_at": bindparam("p3"),
        }
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": now, "p2": now_3, "p3": now_4})


def set_metadata_json_by_attempt_id(conn, value_1, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values({"metadata_json": bindparam("p0")})
        .where((tables["attempts"].c["attempt_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": attempt_id})


def record_result_db(conn, result_created_at, result_created_at_2, attempt_id, task_id):
    stmt = (
        update(tables["attempts"])
        .values(
            {
                "status": case(
                    (
                        (
                            func.json_type(
                                tables["attempts"].c["metadata_json"],
                                sql_constant("$.dispatch.pid"),
                            )
                            == sql_constant("integer")
                        ),
                        sql_constant("running"),
                    ),
                    else_=sql_constant("completed"),
                ),
                "finished_at": case(
                    (
                        (
                            func.json_type(
                                tables["attempts"].c["metadata_json"],
                                sql_constant("$.dispatch.pid"),
                            )
                            == sql_constant("integer")
                        ),
                        null(),
                    ),
                    else_=bindparam("p0"),
                ),
                "metadata_json": func.json_set(
                    tables["attempts"].c["metadata_json"],
                    sql_constant("$.result_received_at"),
                    bindparam("p1"),
                ),
            }
        )
        .where(
            and_(
                tables["attempts"].c["attempt_id"] == bindparam("p2"),
                tables["attempts"].c["task_id"] == bindparam("p3"),
                tables["attempts"]
                .c["status"]
                .in_(
                    [
                        sql_constant("running"),
                        sql_constant("prepared"),
                        sql_constant("stale"),
                    ]
                ),
            )
        )
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": result_created_at,
            "p1": result_created_at_2,
            "p2": attempt_id,
            "p3": task_id,
        },
    )


def record_diagnostic(conn, value_1, value_2, value_3):
    stmt = (
        update(tables["attempts"])
        .values(
            {
                "metadata_json": func.json_set(
                    tables["attempts"].c["metadata_json"],
                    sql_constant("$.submission_validation_counts"),
                    func.json(bindparam("p0")),
                )
            }
        )
        .where(
            and_(
                (tables["attempts"].c["attempt_id"] == bindparam("p1")),
                (tables["attempts"].c["task_id"] == bindparam("p2")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": value_2, "p2": value_3})


def update_for_prepare(conn, now, value_2, now_3, attempt_id, status, metadata_json):
    stmt = (
        update(tables["attempts"])
        .values(
            {
                "status": sql_constant("failed"),
                "finished_at": bindparam("p0"),
                "failure_json": bindparam("p1"),
                "metadata_json": func.json_set(
                    tables["attempts"].c["metadata_json"],
                    sql_constant("$.process_stopped_at"),
                    bindparam("p2"),
                ),
            }
        )
        .where(
            and_(
                tables["attempts"].c["attempt_id"] == bindparam("p3"),
                tables["attempts"].c["status"] == bindparam("p4"),
                tables["attempts"].c["metadata_json"] == bindparam("p5"),
            )
        )
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": now,
            "p1": value_2,
            "p2": now_3,
            "p3": attempt_id,
            "p4": status,
            "p5": metadata_json,
        },
    )


def update_for_prepare_2(conn, now, attempt_id, status, metadata_json):
    stmt = (
        update(tables["attempts"])
        .values(
            {
                "metadata_json": func.json_set(
                    tables["attempts"].c["metadata_json"],
                    sql_constant("$.process_stopped_at"),
                    bindparam("p0"),
                )
            }
        )
        .where(
            and_(
                tables["attempts"].c["attempt_id"] == bindparam("p1"),
                tables["attempts"].c["status"] == bindparam("p2"),
                tables["attempts"].c["metadata_json"] == bindparam("p3"),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": attempt_id, "p2": status, "p3": metadata_json})


def set_status_and_metadata_json_by_attempt_id(conn, status, metadata, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values({"status": bindparam("p0"), "metadata_json": bindparam("p1")})
        .where((tables["attempts"].c["attempt_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": status, "p1": metadata, "p2": attempt_id})


def record_dispatch_result(conn, status, started_at, started_at_3, metadata, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values(
            {
                "status": bindparam("p0"),
                "started_at": bindparam("p1"),
                "heartbeat_at": bindparam("p2"),
                "finished_at": null(),
                "metadata_json": bindparam("p3"),
            }
        )
        .where((tables["attempts"].c["attempt_id"] == bindparam("p4")))
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": status,
            "p1": started_at,
            "p2": started_at_3,
            "p3": metadata,
            "p4": attempt_id,
        },
    )


def record_dispatch_result_2(conn, status, finished_at, failure_json, metadata, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values(
            {
                "status": bindparam("p0"),
                "finished_at": bindparam("p1"),
                "failure_json": bindparam("p2"),
                "metadata_json": bindparam("p3"),
            }
        )
        .where((tables["attempts"].c["attempt_id"] == bindparam("p4")))
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": status,
            "p1": finished_at,
            "p2": failure_json,
            "p3": metadata,
            "p4": attempt_id,
        },
    )


def set_worktree_path_and_branch_name_by_attempt_id(conn, value_1, value_2, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values({"worktree_path": bindparam("p0"), "branch_name": bindparam("p1")})
        .where((tables["attempts"].c["attempt_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": value_2, "p2": attempt_id})


def record_dispatch_result_3(conn, now, now_2, value_3, value_4, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values(
            {
                "status": sql_constant("stale"),
                "finished_at": null(),
                "started_at": func.coalesce(tables["attempts"].c["started_at"], bindparam("p0")),
                "heartbeat_at": bindparam("p1"),
                "metadata_json": bindparam("p2"),
                "failure_json": bindparam("p3"),
            }
        )
        .where((tables["attempts"].c["attempt_id"] == bindparam("p4")))
    )
    return statements.write(
        conn,
        stmt,
        {"p0": now, "p1": now_2, "p2": value_3, "p3": value_4, "p4": attempt_id},
    )


def update_for_dispatch_queued_locked(conn, now, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values({"status": sql_constant("canceled"), "finished_at": bindparam("p0")})
        .where(
            and_(
                (tables["attempts"].c["attempt_id"] == bindparam("p1")),
                (tables["attempts"].c["status"] == sql_constant("prepared")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": attempt_id})


def update_for_dispatch_queued_locked_2(conn, now, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values({"status": sql_constant("failed"), "finished_at": bindparam("p0")})
        .where(
            and_(
                (tables["attempts"].c["attempt_id"] == bindparam("p1")),
                (tables["attempts"].c["status"] == sql_constant("prepared")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": attempt_id})


def update_for_defer_attempt_inspection(conn, value_1, value_2, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values(
            {
                "metadata_json": func.json_set(
                    tables["attempts"].c["metadata_json"],
                    sql_constant("$.supervise"),
                    func.json(bindparam("p0")),
                    sql_constant("$.inspection_retry"),
                    func.json(bindparam("p1")),
                )
            }
        )
        .where((tables["attempts"].c["attempt_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": value_2, "p2": attempt_id})


def update_for_recover_failed_timeout_attempts(conn, run_now, value_2, value_3, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values(
            {
                "finished_at": bindparam("p0"),
                "failure_json": bindparam("p1"),
                "metadata_json": bindparam("p2"),
            }
        )
        .where((tables["attempts"].c["attempt_id"] == bindparam("p3")))
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": value_2, "p2": value_3, "p3": attempt_id})


def update_for_supervise_running_attempts(conn, value_1, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values({"status": sql_constant("stale"), "metadata_json": bindparam("p0")})
        .where((tables["attempts"].c["attempt_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": attempt_id})


def update_for_supervise_running_attempts_2(conn, run_now, value_2, value_3, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values(
            {
                "status": sql_constant("failed"),
                "finished_at": bindparam("p0"),
                "failure_json": bindparam("p1"),
                "metadata_json": bindparam("p2"),
            }
        )
        .where((tables["attempts"].c["attempt_id"] == bindparam("p3")))
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": value_2, "p2": value_3, "p3": attempt_id})


def update_for_defer_attempt_inspection_2(conn, run_now, value_2, run_now_3, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values(
            {
                "status": sql_constant("failed"),
                "finished_at": bindparam("p0"),
                "failure_json": bindparam("p1"),
                "metadata_json": func.json_set(
                    tables["attempts"].c["metadata_json"],
                    sql_constant("$.process_stopped_at"),
                    bindparam("p2"),
                ),
            }
        )
        .where((tables["attempts"].c["attempt_id"] == bindparam("p3")))
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": value_2, "p2": run_now_3, "p3": attempt_id})


def update_for_supervise_running_attempts_3(conn, value_1, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values({"status": sql_constant("running"), "metadata_json": bindparam("p0")})
        .where((tables["attempts"].c["attempt_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": attempt_id})


def update_for_supervise_running_attempts_4(conn, run_now, run_now_2, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values(
            {
                "status": sql_constant("completed"),
                "finished_at": bindparam("p0"),
                "metadata_json": func.json_set(
                    tables["attempts"].c["metadata_json"],
                    sql_constant("$.process_stopped_at"),
                    bindparam("p1"),
                ),
            }
        )
        .where((tables["attempts"].c["attempt_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": run_now_2, "p2": attempt_id})


def update_for_supervise_running_attempts_5(conn, run_now, value_2, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values(
            {
                "status": sql_constant("failed"),
                "finished_at": bindparam("p0"),
                "failure_json": bindparam("p1"),
            }
        )
        .where((tables["attempts"].c["attempt_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": value_2, "p2": attempt_id})


def record_snapshot(conn, created_at, attempt_id, task_id):
    stmt = (
        update(tables["attempts"])
        .values({"heartbeat_at": bindparam("p0")})
        .where(
            and_(
                (tables["attempts"].c["attempt_id"] == bindparam("p1")),
                (tables["attempts"].c["task_id"] == bindparam("p2")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": created_at, "p1": attempt_id, "p2": task_id})


def set_worktree_path_by_attempt_id(conn, worktree_path, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values({"worktree_path": bindparam("p0")})
        .where((tables["attempts"].c["attempt_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": worktree_path, "p1": attempt_id})


def set_heartbeat_at_by_attempt_id(conn, value_1, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values({"heartbeat_at": bindparam("p0")})
        .where((tables["attempts"].c["attempt_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": attempt_id})


def update_for_execute_command(conn, now, task_id):
    stmt = (
        update(tables["attempts"])
        .values({"status": sql_constant("canceled"), "finished_at": bindparam("p0")})
        .where(
            and_(
                (tables["attempts"].c["task_id"] == bindparam("p1")),
                (tables["attempts"].c["status"] == sql_constant("prepared")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": task_id})


def update_for_refresh_prepared_task_locked(conn, value_1, value_2, value_3, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values(
            {
                "worktree_path": bindparam("p0"),
                "branch_name": bindparam("p1"),
                "metadata_json": bindparam("p2"),
            }
        )
        .where((tables["attempts"].c["attempt_id"] == bindparam("p3")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": value_2, "p2": value_3, "p3": attempt_id})


def insert_for_create_task_attempt_and_prompt(
    conn,
    attempt_id,
    task_id,
    attempt_status,
    value_4,
    value_5,
    run_now,
    run_now_7,
    value_8,
):
    stmt = insert(tables["attempts"]).values(
        {
            "attempt_id": bindparam("p0"),
            "task_id": bindparam("p1"),
            "attempt_no": sql_constant(1),
            "status": bindparam("p2"),
            "worktree_path": bindparam("p3"),
            "branch_name": bindparam("p4"),
            "started_at": bindparam("p5"),
            "heartbeat_at": bindparam("p6"),
            "metadata_json": bindparam("p7"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": attempt_id,
            "p1": task_id,
            "p2": attempt_status,
            "p3": value_4,
            "p4": value_5,
            "p5": run_now,
            "p6": run_now_7,
            "p7": value_8,
        },
    )


def insert_for_create_resume_attempt_and_prompt(
    conn,
    attempt_id,
    task_id,
    attempt_no,
    worktree_path,
    branch_name,
    run_now,
    run_now_7,
    value_8,
):
    stmt = insert(tables["attempts"]).values(
        {
            "attempt_id": bindparam("p0"),
            "task_id": bindparam("p1"),
            "attempt_no": bindparam("p2"),
            "status": sql_constant("prepared"),
            "worktree_path": bindparam("p3"),
            "branch_name": bindparam("p4"),
            "started_at": bindparam("p5"),
            "heartbeat_at": bindparam("p6"),
            "metadata_json": bindparam("p7"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": attempt_id,
            "p1": task_id,
            "p2": attempt_no,
            "p3": worktree_path,
            "p4": branch_name,
            "p5": run_now,
            "p6": run_now_7,
            "p7": value_8,
        },
    )


def update_for_refresh_prepared_task_locked_2(conn, run_now, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values({"status": sql_constant("failed"), "finished_at": bindparam("p0")})
        .where((tables["attempts"].c["attempt_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": attempt_id})


def insert_for_prepare_detected_local_tasks(conn, attempt_id, task_id, value_3, value_4, run_now, run_now_6, value_7):
    stmt = insert(tables["attempts"]).values(
        {
            "attempt_id": bindparam("p0"),
            "task_id": bindparam("p1"),
            "attempt_no": sql_constant(1),
            "status": sql_constant("prepared"),
            "worktree_path": bindparam("p2"),
            "branch_name": bindparam("p3"),
            "started_at": bindparam("p4"),
            "heartbeat_at": bindparam("p5"),
            "metadata_json": bindparam("p6"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": attempt_id,
            "p1": task_id,
            "p2": value_3,
            "p3": value_4,
            "p4": run_now,
            "p5": run_now_6,
            "p6": value_7,
        },
    )


def record_remote_terminal_state_locked(conn, run_now, value_2, value_3, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values(
            {
                "status": sql_constant("canceled"),
                "finished_at": bindparam("p0"),
                "failure_json": bindparam("p1"),
                "metadata_json": bindparam("p2"),
            }
        )
        .where((tables["attempts"].c["attempt_id"] == bindparam("p3")))
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": value_2, "p2": value_3, "p3": attempt_id})


def update_for_synthesize(conn, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values(
            {
                "metadata_json": func.json_remove(
                    tables["attempts"].c["metadata_json"],
                    sql_constant("$.submission_validation"),
                )
            }
        )
        .where((tables["attempts"].c["attempt_id"] == bindparam("p0")))
    )
    return statements.write(conn, stmt, {"p0": attempt_id})


def update_for_run(conn, value_1, attempt_id):
    stmt = (
        update(tables["attempts"])
        .values(
            {
                "metadata_json": func.json_set(
                    tables["attempts"].c["metadata_json"],
                    sql_constant("$.submission_validation"),
                    func.json(bindparam("p0")),
                )
            }
        )
        .where((tables["attempts"].c["attempt_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": attempt_id})


def insert_fixture(conn, rows):
    stmt = insert(tables["attempts"]).values(
        {
            "attempt_id": bindparam("p0"),
            "task_id": bindparam("p1"),
            "attempt_no": literal(1),
            "status": literal("completed"),
            "started_at": bindparam("p2"),
            "heartbeat_at": bindparam("p3"),
        }
    )
    return statements.write_many(conn, stmt, rows)


def cancel_execution_filtered(conn, items, value_1):
    query1 = (
        select(tables["tasks"].c["task_id"])
        .select_from(tables["tasks"])
        .where(tables["tasks"].c["workstream_id"].in_(bindparam("items", expanding=True)))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    stmt = (
        update(tables["attempts"])
        .values({"status": sql_constant("canceled"), "finished_at": bindparam("p0")})
        .where(
            and_(
                tables["attempts"].c["task_id"].in_(query1),
                tables["attempts"]
                .c["status"]
                .in_(
                    [
                        sql_constant("prepared"),
                        sql_constant("running"),
                        sql_constant("stale"),
                    ]
                ),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": value_1, "items": items})
