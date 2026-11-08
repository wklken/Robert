"""Shared stores operations for tasks."""

from robert_agent.foundation.contracts.delivery import OutputType
from robert_agent.foundation.contracts.work import TaskLifecycle

from sqlalchemy import literal

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import (
    select,
    update,
    delete,
    bindparam,
    and_,
    not_,
    func,
    LABEL_STYLE_NONE,
)
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_canary_fixture(conn, source_key, now, now_3):
    stmt = insert(tables["tasks"]).values(
        {
            "task_id": sql_constant("task-production-canary"),
            "workstream_id": bindparam("p0"),
            "lifecycle": sql_constant(TaskLifecycle.COMPLETED.value),
            "priority": sql_constant("P2"),
            "route_id": sql_constant("production-write-canary"),
            "expected_output": sql_constant(OutputType.COMMENT_ANALYSIS.value),
            "created_at": bindparam("p1"),
            "updated_at": bindparam("p2"),
        }
    )
    return statements.write(conn, stmt, {"p0": source_key, "p1": now, "p2": now_3})


def update_for_audit_completed_results(conn, result_id, task_id):
    stmt = (
        update(tables["tasks"])
        .values(
            {
                "metadata_json": func.json_set(
                    tables["tasks"].c["metadata_json"],
                    sql_constant("$.audit_retry_required"),
                    bindparam("p0"),
                )
            }
        )
        .where((tables["tasks"].c["task_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": result_id, "p1": task_id})


def update_for_prepare(conn, value_1, now, task_id):
    stmt = (
        update(tables["tasks"])
        .values(
            {
                "metadata_json": func.json_set(
                    tables["tasks"].c["metadata_json"],
                    sql_constant("$.execution_attention"),
                    func.json(bindparam("p0")),
                ),
                "updated_at": bindparam("p1"),
            }
        )
        .where((tables["tasks"].c["task_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": now, "p2": task_id})


def update_for_dispatch_queued_locked(conn, task_id):
    stmt = (
        update(tables["tasks"])
        .values(
            {
                "metadata_json": func.json_set(
                    tables["tasks"].c["metadata_json"],
                    sql_constant("$.dispatch_wait"),
                    sql_constant("manual_worker_unavailable"),
                )
            }
        )
        .where((tables["tasks"].c["task_id"] == bindparam("p0")))
    )
    return statements.write(conn, stmt, {"p0": task_id})


def update_for_dispatch_queued_locked_2(conn, now, task_id):
    stmt = (
        update(tables["tasks"])
        .values({"lifecycle": sql_constant(TaskLifecycle.CANCELED.value), "updated_at": bindparam("p0")})
        .where((tables["tasks"].c["task_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": now, "p1": task_id})


def update_for_supervise_running_attempts(conn, run_now, task_id):
    stmt = (
        update(tables["tasks"])
        .values({"lifecycle": sql_constant(TaskLifecycle.FAILED.value), "updated_at": bindparam("p0")})
        .where((tables["tasks"].c["task_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": task_id})


def set_metadata_json_by_task_id(conn, value_1, task_id):
    stmt = (
        update(tables["tasks"])
        .values({"metadata_json": bindparam("p0")})
        .where((tables["tasks"].c["task_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": task_id})


def insert_for_create_task(
    conn,
    task_id,
    workstream_id,
    parent_task_id,
    priority,
    routing_mode,
    requested_worker,
    now,
    now_8,
    value_9,
):
    stmt = insert(tables["tasks"]).values(
        {
            "task_id": bindparam("p0"),
            "workstream_id": bindparam("p1"),
            "lifecycle": sql_constant(TaskLifecycle.DETECTED.value),
            "parent_task_id": bindparam("p2"),
            "priority": bindparam("p3"),
            "routing_mode": bindparam("p4"),
            "requested_worker": bindparam("p5"),
            "created_at": bindparam("p6"),
            "updated_at": bindparam("p7"),
            "metadata_json": bindparam("p8"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": task_id,
            "p1": workstream_id,
            "p2": parent_task_id,
            "p3": priority,
            "p4": routing_mode,
            "p5": requested_worker,
            "p6": now,
            "p7": now_8,
            "p8": value_9,
        },
    )


def update_for_finish_parent_for_child(conn, now, task_id):
    stmt = (
        update(tables["tasks"])
        .values({"lifecycle": sql_constant(TaskLifecycle.COMPLETED.value), "updated_at": bindparam("p0")})
        .where((tables["tasks"].c["task_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": now, "p1": task_id})


def update_for_retrigger_failed_github_task(conn, value_1, recovery_now, task_id):
    stmt = (
        update(tables["tasks"])
        .values(
            {
                "lifecycle": sql_constant(TaskLifecycle.FAILED.value),
                "metadata_json": bindparam("p0"),
                "updated_at": bindparam("p1"),
            }
        )
        .where((tables["tasks"].c["task_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": recovery_now, "p2": task_id})


def update_for_execute_command(conn, now, task_id):
    stmt = (
        update(tables["tasks"])
        .values(
            {
                "metadata_json": func.json_remove(
                    tables["tasks"].c["metadata_json"],
                    sql_constant("$.audit_retry_required"),
                ),
                "updated_at": bindparam("p0"),
            }
        )
        .where((tables["tasks"].c["task_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": now, "p1": task_id})


def update_for_execute_command_2(conn, value_1, now, task_id):
    stmt = (
        update(tables["tasks"])
        .values(
            {
                "metadata_json": func.json_set(
                    tables["tasks"].c["metadata_json"],
                    sql_constant("$.pending_handoff"),
                    func.json(bindparam("p0")),
                ),
                "updated_at": bindparam("p1"),
            }
        )
        .where((tables["tasks"].c["task_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": now, "p2": task_id})


def update_for_save_pending_handoff(conn, value_1, task_id):
    stmt = (
        update(tables["tasks"])
        .values(
            {
                "metadata_json": func.json_set(
                    tables["tasks"].c["metadata_json"],
                    sql_constant("$.pending_handoff"),
                    func.json(bindparam("p0")),
                )
            }
        )
        .where((tables["tasks"].c["task_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": task_id})


def update_for_create_child_task_for_recommended_route(conn, value_1, parent_task_id):
    stmt = (
        update(tables["tasks"])
        .values(
            {
                "metadata_json": func.json_set(
                    tables["tasks"].c["metadata_json"],
                    sql_constant("$.classification_handoff"),
                    func.json(bindparam("p0")),
                )
            }
        )
        .where((tables["tasks"].c["task_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": parent_task_id})


def mark_task_waiting_for_publish(conn, run_now, task_id):
    stmt = (
        update(tables["tasks"])
        .values({"lifecycle": sql_constant(TaskLifecycle.RUNNING.value), "updated_at": bindparam("p0")})
        .where((tables["tasks"].c["task_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": task_id})


def mark_task_waiting_for_user(conn, run_now, task_id):
    stmt = (
        update(tables["tasks"])
        .values(
            {
                "lifecycle": sql_constant(TaskLifecycle.WAITING_FOR_USER.value),
                "updated_at": bindparam("p0"),
            }
        )
        .where((tables["tasks"].c["task_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": task_id})


def update_for_refresh_prepared_task_locked(conn, route_id, expected_output, run_now, task_id):
    stmt = (
        update(tables["tasks"])
        .values(
            {
                "route_id": bindparam("p0"),
                "expected_output": bindparam("p1"),
                "updated_at": bindparam("p2"),
            }
        )
        .where((tables["tasks"].c["task_id"] == bindparam("p3")))
    )
    return statements.write(
        conn,
        stmt,
        {"p0": route_id, "p1": expected_output, "p2": run_now, "p3": task_id},
    )


def update_for_refresh_prepared_task_locked_2(conn, input_policy, task_id, request_event_fingerprint):
    stmt = (
        update(tables["tasks"])
        .values(
            {
                "metadata_json": func.json_set(
                    tables["tasks"].c["metadata_json"],
                    sql_constant("$.review_input_policy"),
                    bindparam("p0"),
                    sql_constant("$.request_event_fingerprint"),
                    bindparam("request_event_fingerprint"),
                )
            }
        )
        .where((tables["tasks"].c["task_id"] == bindparam("p1")))
    )
    return statements.write(
        conn, stmt, {"p0": input_policy, "p1": task_id, "request_event_fingerprint": request_event_fingerprint}
    )


def update_for_create_task_attempt_and_prompt(
    conn, task_lifecycle, route_id, expected_output, run_now, value_5, task_id
):
    stmt = (
        update(tables["tasks"])
        .values(
            {
                "lifecycle": bindparam("p0"),
                "route_id": bindparam("p1"),
                "expected_output": bindparam("p2"),
                "updated_at": bindparam("p3"),
                "metadata_json": bindparam("p4"),
            }
        )
        .where((tables["tasks"].c["task_id"] == bindparam("p5")))
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": task_lifecycle,
            "p1": route_id,
            "p2": expected_output,
            "p3": run_now,
            "p4": value_5,
            "p5": task_id,
        },
    )


def insert_for_create_task_attempt_and_prompt(
    conn,
    task_id,
    workstream_id,
    value_3,
    value_4,
    parent_task_id,
    route_id,
    expected_output,
    run_now,
    run_now_9,
    value_10,
):
    stmt = insert(tables["tasks"]).values(
        {
            "task_id": bindparam("p0"),
            "workstream_id": bindparam("p1"),
            "task_kind": bindparam("p2"),
            "lifecycle": bindparam("p3"),
            "parent_task_id": bindparam("p4"),
            "priority": sql_constant("P1"),
            "route_id": bindparam("p5"),
            "expected_output": bindparam("p6"),
            "created_at": bindparam("p7"),
            "updated_at": bindparam("p8"),
            "metadata_json": bindparam("p9"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": task_id,
            "p1": workstream_id,
            "p2": value_3,
            "p3": value_4,
            "p4": parent_task_id,
            "p5": route_id,
            "p6": expected_output,
            "p7": run_now,
            "p8": run_now_9,
            "p9": value_10,
        },
    )


def delete_for_create_task_attempt_and_prompt(conn, task_id):
    stmt = delete(tables["tasks"]).where((tables["tasks"].c["task_id"] == bindparam("p0")))
    return statements.write(conn, stmt, {"p0": task_id})


def update_for_prepare_detected_local_tasks(
    conn,
    route_id,
    expected_output,
    priority,
    routing_mode,
    requested_worker,
    run_now,
    value_7,
    task_id,
):
    stmt = (
        update(tables["tasks"])
        .values(
            {
                "lifecycle": sql_constant(TaskLifecycle.QUEUED.value),
                "route_id": bindparam("p0"),
                "expected_output": bindparam("p1"),
                "priority": bindparam("p2"),
                "routing_mode": bindparam("p3"),
                "requested_worker": bindparam("p4"),
                "updated_at": bindparam("p5"),
                "metadata_json": bindparam("p6"),
            }
        )
        .where((tables["tasks"].c["task_id"] == bindparam("p7")))
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": route_id,
            "p1": expected_output,
            "p2": priority,
            "p3": routing_mode,
            "p4": requested_worker,
            "p5": run_now,
            "p6": value_7,
            "p7": task_id,
        },
    )


def set_route_id_and_expected_output_by_task_id(conn, route_id, expected_output, task_id):
    stmt = (
        update(tables["tasks"])
        .values({"route_id": bindparam("p0"), "expected_output": bindparam("p1")})
        .where((tables["tasks"].c["task_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": route_id, "p1": expected_output, "p2": task_id})


def record_remote_terminal_state_locked(conn, run_now, value_2, task_id):
    stmt = (
        update(tables["tasks"])
        .values(
            {
                "lifecycle": sql_constant(TaskLifecycle.CANCELED.value),
                "updated_at": bindparam("p0"),
                "metadata_json": bindparam("p1"),
            }
        )
        .where(
            and_(
                (tables["tasks"].c["task_id"] == bindparam("p2")),
                tables["tasks"]
                .c["lifecycle"]
                .in_(
                    [
                        sql_constant(TaskLifecycle.DETECTED.value),
                        sql_constant(TaskLifecycle.AUTHORIZED.value),
                        sql_constant(TaskLifecycle.CLASSIFIED.value),
                        sql_constant(TaskLifecycle.QUEUED.value),
                        sql_constant(TaskLifecycle.RUNNING.value),
                        sql_constant(TaskLifecycle.WAITING_FOR_USER.value),
                    ]
                ),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": value_2, "p2": task_id})


def update_for_clear_repository_busy(conn, repo_id):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    query3 = (
        select(table1.c["task_id"])
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
        )
        .where((table2.c["repo_id"] == bindparam("p0")))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    stmt = (
        update(tables["tasks"])
        .values({"metadata_json": func.json_remove(tables["tasks"].c["metadata_json"], sql_constant("$.wait_state"))})
        .where(
            and_(
                tables["tasks"].c["task_id"].in_(query3),
                (
                    func.json_extract(
                        tables["tasks"].c["metadata_json"],
                        sql_constant("$.wait_state.reason_code"),
                    )
                    == sql_constant("repository_busy")
                ),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": repo_id})


def mark_repository_busy(conn, now, expires_at, task_id):
    stmt = (
        update(tables["tasks"])
        .values(
            {
                "metadata_json": func.json_set(
                    tables["tasks"].c["metadata_json"],
                    sql_constant("$.wait_state.reason_code"),
                    sql_constant("repository_busy"),
                    sql_constant("$.wait_state.since"),
                    func.coalesce(
                        func.json_extract(
                            tables["tasks"].c["metadata_json"],
                            sql_constant("$.wait_state.since"),
                        ),
                        bindparam("p0"),
                    ),
                    sql_constant("$.wait_state.lease_expires_at"),
                    bindparam("p1"),
                )
            }
        )
        .where((tables["tasks"].c["task_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": now, "p1": expires_at, "p2": task_id})


def insert_fixture(conn, rows):
    stmt = insert(tables["tasks"]).values(
        {
            "task_id": bindparam("p0"),
            "workstream_id": literal("github:x/y#1"),
            "lifecycle": bindparam("p1"),
            "priority": literal("P1"),
            "created_at": bindparam("p2"),
            "updated_at": bindparam("p3"),
        }
    )
    return statements.write_many(conn, stmt, rows)


def cancel_execution_filtered(conn, items, value_1):
    stmt = (
        update(tables["tasks"])
        .values({"lifecycle": sql_constant(TaskLifecycle.CANCELED.value), "updated_at": bindparam("p0")})
        .where(
            and_(
                tables["tasks"].c["workstream_id"].in_(bindparam("items", expanding=True)),
                not_(
                    tables["tasks"]
                    .c["lifecycle"]
                    .in_(
                        [
                            sql_constant(TaskLifecycle.COMPLETED.value),
                            sql_constant(TaskLifecycle.IGNORED.value),
                            sql_constant(TaskLifecycle.CANCELED.value),
                        ]
                    )
                ),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": value_1, "items": items})


def record_local_preparation_retry(conn, task_id, failure_json):
    task = tables["tasks"]
    return statements.write(
        conn,
        update(task)
        .where(task.c.task_id == task_id)
        .values(
            metadata_json=func.json_set(
                task.c.metadata_json, sql_constant("$.preparation_retry"), func.json(failure_json)
            )
        ),
    )
