"""Shared stores operations for wakeups."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, delete, bindparam, and_, not_, null, func
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def update_for_consume_fact(conn, now, value_2):
    stmt = (
        update(tables["wakeups"])
        .values({"status": sql_constant("consumed"), "updated_at": bindparam("p0")})
        .where(
            and_(
                (tables["wakeups"].c["dedupe_key"] == bindparam("p1")),
                (tables["wakeups"].c["status"] == sql_constant("pending")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": value_2})


def update_for_publish_ready_actions(conn, value_1, task_id):
    stmt = (
        update(tables["wakeups"])
        .values({"status": sql_constant("consumed"), "updated_at": bindparam("p0")})
        .where(
            and_(
                tables["wakeups"].c["task_id"] == bindparam("p1"),
                tables["wakeups"].c["status"] == sql_constant("pending"),
                func.json_extract(tables["wakeups"].c["metadata_json"], sql_constant("$.kind"))
                == sql_constant("publication_retry"),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": task_id})


def insert_worker_result_wakeup(
    conn,
    wakeup_id,
    value_2,
    result_id,
    value_4,
    task_id,
    attempt_id,
    result_id_7,
    created_at,
    created_at_9,
    created_at_10,
    value_11,
):
    stmt = (
        insert(tables["wakeups"])
        .values(
            {
                "wakeup_id": bindparam("p0"),
                "repo_id": bindparam("p1"),
                "reason": sql_constant("worker_result_ready"),
                "dedupe_key": bindparam("p2"),
                "work_item_id": bindparam("p3"),
                "task_id": bindparam("p4"),
                "attempt_id": bindparam("p5"),
                "result_id": bindparam("p6"),
                "status": sql_constant("pending"),
                "not_before_at": bindparam("p7"),
                "created_at": bindparam("p8"),
                "updated_at": bindparam("p9"),
                "metadata_json": bindparam("p10"),
            }
        )
        .prefix_with("OR IGNORE", dialect="sqlite")
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": wakeup_id,
            "p1": value_2,
            "p2": result_id,
            "p3": value_4,
            "p4": task_id,
            "p5": attempt_id,
            "p6": result_id_7,
            "p7": created_at,
            "p8": created_at_9,
            "p9": created_at_10,
            "p10": value_11,
        },
    )


def update_for_cancel_execution(conn, now, work_item_id):
    stmt = (
        update(tables["wakeups"])
        .values({"status": sql_constant("canceled"), "updated_at": bindparam("p0")})
        .where(
            and_(
                (tables["wakeups"].c["work_item_id"] == bindparam("p1")),
                (tables["wakeups"].c["status"] == sql_constant("pending")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": work_item_id})


def update_for_finalize_superseded_result(conn, now, result_id):
    stmt = (
        update(tables["wakeups"])
        .values({"status": sql_constant("consumed"), "updated_at": bindparam("p0")})
        .where(
            and_(
                (tables["wakeups"].c["result_id"] == bindparam("p1")),
                (tables["wakeups"].c["status"] == sql_constant("pending")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": result_id})


def record_retrigger_preparation_failure(conn, value_1, value_2, attempt_count, value_4, now, wakeup_id):
    stmt = (
        update(tables["wakeups"])
        .values(
            {
                "status": bindparam("p0"),
                "not_before_at": bindparam("p1"),
                "attempt_count": bindparam("p2"),
                "metadata_json": bindparam("p3"),
                "updated_at": bindparam("p4"),
            }
        )
        .where(
            and_(
                (tables["wakeups"].c["wakeup_id"] == bindparam("p5")),
                (tables["wakeups"].c["status"] == sql_constant("pending")),
            )
        )
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": value_1,
            "p1": value_2,
            "p2": attempt_count,
            "p3": value_4,
            "p4": now,
            "p5": wakeup_id,
        },
    )


def update_for_prepare_detected_local_tasks(conn, run_id, run_now, work_item_id, task_id):
    stmt = (
        update(tables["wakeups"])
        .values(
            {
                "status": sql_constant("consumed"),
                "consumed_run_id": bindparam("p0"),
                "updated_at": bindparam("p1"),
            }
        )
        .where(
            and_(
                tables["wakeups"].c["work_item_id"] == bindparam("p2"),
                tables["wakeups"].c["task_id"] == bindparam("p3"),
                tables["wakeups"].c["reason"] == sql_constant("manual_operator_request"),
                tables["wakeups"].c["status"] == sql_constant("pending"),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": run_id, "p1": run_now, "p2": work_item_id, "p3": task_id})


def update_for_prepare_github_retrigger_tasks(conn, run_id, run_now, wakeup_id):
    stmt = (
        update(tables["wakeups"])
        .values(
            {
                "status": sql_constant("consumed"),
                "consumed_run_id": bindparam("p0"),
                "updated_at": bindparam("p1"),
            }
        )
        .where(
            and_(
                (tables["wakeups"].c["wakeup_id"] == bindparam("p2")),
                (tables["wakeups"].c["status"] == sql_constant("pending")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": run_id, "p1": run_now, "p2": wakeup_id})


def update_for_prepare_github_operator_tasks(conn, run_id, run_now, wakeup_id):
    stmt = (
        update(tables["wakeups"])
        .values(
            {
                "status": sql_constant("consumed"),
                "consumed_run_id": bindparam("p0"),
                "updated_at": bindparam("p1"),
            }
        )
        .where((tables["wakeups"].c["wakeup_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": run_id, "p1": run_now, "p2": wakeup_id})


def update_for_prepare_github_retrigger_tasks_2(conn, run_now, wakeup_id):
    stmt = (
        update(tables["wakeups"])
        .values({"status": sql_constant("canceled"), "updated_at": bindparam("p0")})
        .where((tables["wakeups"].c["wakeup_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": wakeup_id})


def update_for_prepare_detected_local_tasks_2(conn, run_now, task_id):
    stmt = (
        update(tables["wakeups"])
        .values({"status": sql_constant("consumed"), "updated_at": bindparam("p0")})
        .where(
            and_(
                (tables["wakeups"].c["task_id"] == bindparam("p1")),
                (tables["wakeups"].c["status"] == sql_constant("pending")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": task_id})


def update_for_scan_repo(conn, repo_id):
    stmt = (
        update(tables["wakeups"])
        .values({"status": sql_constant("consumed")})
        .where(
            and_(
                tables["wakeups"].c["repo_id"] == bindparam("p0"),
                func.json_extract(tables["wakeups"].c["metadata_json"], sql_constant("$.kind"))
                == sql_constant("github_context_refresh"),
                tables["wakeups"].c["status"] == sql_constant("pending"),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": repo_id})


def update_for_fail_batch(conn, value_1, value_2, value_3, batch_id):
    stmt = (
        update(tables["wakeups"])
        .values(
            {
                "status": bindparam("p0"),
                "not_before_at": bindparam("p1"),
                "updated_at": bindparam("p2"),
            }
        )
        .where(
            and_(
                (
                    func.json_extract(tables["wakeups"].c["metadata_json"], sql_constant("$.kind"))
                    == sql_constant("github_observation_ready")
                ),
                (tables["wakeups"].c["dedupe_key"] == bindparam("p3")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": value_2, "p2": value_3, "p3": batch_id})


def update_for_retry_batch(conn, value_1, batch_id):
    stmt = (
        update(tables["wakeups"])
        .values({"status": sql_constant("pending"), "not_before_at": bindparam("p0")})
        .where(
            and_(
                (
                    func.json_extract(tables["wakeups"].c["metadata_json"], sql_constant("$.kind"))
                    == sql_constant("github_observation_ready")
                ),
                (tables["wakeups"].c["dedupe_key"] == bindparam("p1")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": batch_id})


def delete_for_finish_batch(conn):
    stmt = delete(tables["wakeups"]).where(
        and_(
            (tables["wakeups"].c["status"] == sql_constant("consumed")),
            func.json_extract(tables["wakeups"].c["metadata_json"], sql_constant("$.kind")).in_(
                [
                    sql_constant("github_observation_ready"),
                    sql_constant("github_context_refresh"),
                ]
            ),
        )
    )
    return statements.write(conn, stmt, {})


def update_for_finish_batch(conn, batch_id):
    stmt = (
        update(tables["wakeups"])
        .values({"status": sql_constant("consumed")})
        .where(
            and_(
                (
                    func.json_extract(tables["wakeups"].c["metadata_json"], sql_constant("$.kind"))
                    == sql_constant("github_observation_ready")
                ),
                (tables["wakeups"].c["dedupe_key"] == bindparam("p0")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": batch_id})


def insert_for_request_wakeup(
    conn,
    wakeup_id,
    repo_id,
    reason,
    dedupe_key,
    work_item_id,
    task_id,
    attempt_id,
    result_id,
    source_run_id,
    due_at,
    expires_at,
    now,
    now_13,
    metadata_json,
):
    stmt = insert(tables["wakeups"]).values(
        {
            "wakeup_id": bindparam("p0"),
            "repo_id": bindparam("p1"),
            "reason": bindparam("p2"),
            "dedupe_key": bindparam("p3"),
            "work_item_id": bindparam("p4"),
            "task_id": bindparam("p5"),
            "attempt_id": bindparam("p6"),
            "result_id": bindparam("p7"),
            "source_run_id": bindparam("p8"),
            "status": sql_constant("pending"),
            "not_before_at": bindparam("p9"),
            "expires_at": bindparam("p10"),
            "created_at": bindparam("p11"),
            "updated_at": bindparam("p12"),
            "metadata_json": bindparam("p13"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": wakeup_id,
            "p1": repo_id,
            "p2": reason,
            "p3": dedupe_key,
            "p4": work_item_id,
            "p5": task_id,
            "p6": attempt_id,
            "p7": result_id,
            "p8": source_run_id,
            "p9": due_at,
            "p10": expires_at,
            "p11": now,
            "p12": now_13,
            "p13": metadata_json,
        },
    )


def update_for_expire_due_wakeups(conn, now, now_2):
    stmt = (
        update(tables["wakeups"])
        .values({"status": sql_constant("expired"), "updated_at": bindparam("p0")})
        .where(
            and_(
                tables["wakeups"].c["status"] == sql_constant("pending"),
                not_(tables["wakeups"].c["expires_at"].is_(null())),
                tables["wakeups"].c["expires_at"] <= bindparam("p1"),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": now_2})


def consume_wakeups_for_results_filtered(conn, items, value_1, value_2):
    stmt = (
        update(tables["wakeups"])
        .values(
            {
                "status": sql_constant("consumed"),
                "consumed_run_id": bindparam("p0"),
                "updated_at": bindparam("p1"),
            }
        )
        .where(
            and_(
                tables["wakeups"].c["reason"] == sql_constant("worker_result_ready"),
                tables["wakeups"].c["status"] == sql_constant("pending"),
                tables["wakeups"].c["result_id"].in_(bindparam("items", expanding=True)),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": value_2, "items": items})
