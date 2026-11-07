"""Shared stores operations for workstreams."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, update, bindparam, null, LABEL_STYLE_NONE
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_fixture(conn, now, now_2):
    stmt = insert(tables["workstreams"]).values(
        {
            "workstream_id": sql_constant("github:x/y#1"),
            "repo_id": sql_constant("repo-1"),
            "primary_source_id": sql_constant("source-1"),
            "lifecycle": sql_constant("active"),
            "created_at": bindparam("p0"),
            "updated_at": bindparam("p1"),
        }
    )
    return statements.write(conn, stmt, {"p0": now, "p1": now_2})


def insert_canary_fixture(conn, source_key, now, now_3):
    stmt = insert(tables["workstreams"]).values(
        {
            "workstream_id": bindparam("p0"),
            "repo_id": sql_constant("repo-production-canary"),
            "primary_source_id": sql_constant("source-production-canary"),
            "lifecycle": sql_constant("completed"),
            "created_at": bindparam("p1"),
            "updated_at": bindparam("p2"),
        }
    )
    return statements.write(conn, stmt, {"p0": source_key, "p1": now, "p2": now_3})


def update_for_dispatch_queued_locked(conn, now, task_id):
    stmt = (
        update(tables["workstreams"])
        .values(
            {
                "lifecycle": sql_constant("completed"),
                "active_task_id": null(),
                "updated_at": bindparam("p0"),
            }
        )
        .where((tables["workstreams"].c["active_task_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": now, "p1": task_id})


def update_for_supervise_running_attempts(conn, run_now, workstream_id):
    stmt = (
        update(tables["workstreams"])
        .values(
            {
                "lifecycle": sql_constant("failed"),
                "active_task_id": null(),
                "updated_at": bindparam("p0"),
            }
        )
        .where((tables["workstreams"].c["workstream_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": workstream_id})


def set_updated_at_by_workstream_id(conn, run_now, workstream_id):
    stmt = (
        update(tables["workstreams"])
        .values({"updated_at": bindparam("p0")})
        .where((tables["workstreams"].c["workstream_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": workstream_id})


def update_for_create_task(conn, task_id, now, workstream_id):
    stmt = (
        update(tables["workstreams"])
        .values(
            {
                "lifecycle": sql_constant("active"),
                "active_task_id": bindparam("p0"),
                "updated_at": bindparam("p1"),
            }
        )
        .where((tables["workstreams"].c["workstream_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": task_id, "p1": now, "p2": workstream_id})


def insert_for_activate(conn, workstream_id, repo_id, now, now_4, value_5):
    stmt = insert(tables["workstreams"]).values(
        {
            "workstream_id": bindparam("p0"),
            "repo_id": bindparam("p1"),
            "lifecycle": sql_constant("active"),
            "active_task_id": null(),
            "created_at": bindparam("p2"),
            "updated_at": bindparam("p3"),
            "metadata_json": bindparam("p4"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {"p0": workstream_id, "p1": repo_id, "p2": now, "p3": now_4, "p4": value_5},
    )


def update_for_execute_command(conn, now, workstream_id):
    stmt = (
        update(tables["workstreams"])
        .values(
            {
                "lifecycle": sql_constant("completed"),
                "active_task_id": null(),
                "updated_at": bindparam("p0"),
            }
        )
        .where((tables["workstreams"].c["workstream_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": now, "p1": workstream_id})


def insert_for_materialize_open_pr_workstream(
    conn, pr_source_key, repo_id, pr_source_id, origin_workstream_id, run_now, run_now_6
):
    stmt = insert(tables["workstreams"]).values(
        {
            "workstream_id": bindparam("p0"),
            "repo_id": bindparam("p1"),
            "primary_source_id": bindparam("p2"),
            "origin_workstream_id": bindparam("p3"),
            "lifecycle": sql_constant("completed"),
            "active_task_id": null(),
            "created_at": bindparam("p4"),
            "updated_at": bindparam("p5"),
            "metadata_json": sql_constant("{}"),
        }
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["workstream_id"],
        set_={
            "origin_workstream_id": stmt.excluded["origin_workstream_id"],
            "primary_source_id": stmt.excluded["primary_source_id"],
            "updated_at": stmt.excluded["updated_at"],
        },
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": pr_source_key,
            "p1": repo_id,
            "p2": pr_source_id,
            "p3": origin_workstream_id,
            "p4": run_now,
            "p5": run_now_6,
        },
    )


def update_for_finalize_failed_task(conn, run_now, value_2, workstream_id):
    stmt = (
        update(tables["workstreams"])
        .values(
            {
                "lifecycle": sql_constant("failed"),
                "active_task_id": null(),
                "updated_at": bindparam("p0"),
                "metadata_json": bindparam("p1"),
            }
        )
        .where((tables["workstreams"].c["workstream_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": value_2, "p2": workstream_id})


def mark_task_waiting_for_user(conn, task_id, run_now, workstream_id):
    stmt = (
        update(tables["workstreams"])
        .values(
            {
                "lifecycle": sql_constant("waiting_for_user"),
                "active_task_id": bindparam("p0"),
                "updated_at": bindparam("p1"),
            }
        )
        .where((tables["workstreams"].c["workstream_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": task_id, "p1": run_now, "p2": workstream_id})


def set_metadata_json_and_updated_at_by_workstream_id(conn, value_1, run_now, workstream_id):
    stmt = (
        update(tables["workstreams"])
        .values({"metadata_json": bindparam("p0"), "updated_at": bindparam("p1")})
        .where((tables["workstreams"].c["workstream_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": run_now, "p2": workstream_id})


def insert_for_create_task_attempt_and_prompt(
    conn, workstream_id, repo_id, source_id, lifecycle, task_id, run_now, run_now_7
):
    stmt = insert(tables["workstreams"]).values(
        {
            "workstream_id": bindparam("p0"),
            "repo_id": bindparam("p1"),
            "primary_source_id": bindparam("p2"),
            "lifecycle": bindparam("p3"),
            "active_task_id": bindparam("p4"),
            "created_at": bindparam("p5"),
            "updated_at": bindparam("p6"),
        }
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["workstream_id"],
        set_={
            "lifecycle": stmt.excluded["lifecycle"],
            "active_task_id": stmt.excluded["active_task_id"],
            "updated_at": stmt.excluded["updated_at"],
        },
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": workstream_id,
            "p1": repo_id,
            "p2": source_id,
            "p3": lifecycle,
            "p4": task_id,
            "p5": run_now,
            "p6": run_now_7,
        },
    )


def record_remote_terminal_state_locked(conn, workstream_lifecycle, run_now, value_3, workstream_id):
    stmt = (
        update(tables["workstreams"])
        .values(
            {
                "lifecycle": bindparam("p0"),
                "active_task_id": null(),
                "updated_at": bindparam("p1"),
                "metadata_json": bindparam("p2"),
            }
        )
        .where((tables["workstreams"].c["workstream_id"] == bindparam("p3")))
    )
    return statements.write(
        conn,
        stmt,
        {"p0": workstream_lifecycle, "p1": run_now, "p2": value_3, "p3": workstream_id},
    )


def record_remote_terminal_state_locked_2(conn, run_now, work_item_id):
    query1 = (
        select(tables["work_items"].c["workstream_id"])
        .select_from(tables["work_items"])
        .where((tables["work_items"].c["work_item_id"] == bindparam("p1")))
        .correlate_except(tables["work_items"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    stmt = (
        update(tables["workstreams"])
        .values(
            {
                "lifecycle": sql_constant("completed"),
                "active_task_id": null(),
                "updated_at": bindparam("p0"),
            }
        )
        .where((tables["workstreams"].c["workstream_id"] == query1.scalar_subquery()))
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": work_item_id})


def cancel_execution_filtered(conn, items, value_1):
    stmt = (
        update(tables["workstreams"])
        .values(
            {
                "lifecycle": sql_constant("canceled"),
                "active_task_id": null(),
                "updated_at": bindparam("p0"),
            }
        )
        .where(tables["workstreams"].c["workstream_id"].in_(bindparam("items", expanding=True)))
    )
    return statements.write(conn, stmt, {"p0": value_1, "items": items})
