"""Shared stores operations for work_items."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, bindparam, and_, not_, null, func
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_for_ensure_github_work_item(
    conn,
    work_item_id,
    repo_id,
    title,
    root_source_id,
    root_workstream_id,
    value_6,
    created_by,
    activated_at,
    created_at,
    now,
):
    stmt = insert(tables["work_items"]).values(
        {
            "work_item_id": bindparam("p0"),
            "repo_id": bindparam("p1"),
            "title": bindparam("p2"),
            "description": sql_constant(""),
            "priority": sql_constant("P1"),
            "origin_type": sql_constant("github"),
            "origin_source_id": bindparam("p3"),
            "routing_mode": sql_constant("auto"),
            "requested_worker": null(),
            "workstream_id": bindparam("p4"),
            "creation_idempotency_key": bindparam("p5"),
            "created_by": bindparam("p6"),
            "activated_at": bindparam("p7"),
            "version": sql_constant(1),
            "created_at": bindparam("p8"),
            "updated_at": bindparam("p9"),
            "metadata_json": sql_constant("{}"),
        }
    )
    stmt = stmt.on_conflict_do_nothing()
    return statements.write(
        conn,
        stmt,
        {
            "p0": work_item_id,
            "p1": repo_id,
            "p2": title,
            "p3": root_source_id,
            "p4": root_workstream_id,
            "p5": value_6,
            "p6": created_by,
            "p7": activated_at,
            "p8": created_at,
            "p9": now,
        },
    )


def record_github_response(conn, now, work_item_id):
    stmt = (
        update(tables["work_items"])
        .values(
            {
                "version": (tables["work_items"].c["version"] + sql_constant(1)),
                "updated_at": bindparam("p0"),
            }
        )
        .where((tables["work_items"].c["work_item_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": now, "p1": work_item_id})


def update_for_activate(conn, workstream_id, now, now_3, version, work_item_id):
    stmt = (
        update(tables["work_items"])
        .values(
            {
                "workstream_id": bindparam("p0"),
                "activated_at": bindparam("p1"),
                "updated_at": bindparam("p2"),
                "version": bindparam("p3"),
            }
        )
        .where((tables["work_items"].c["work_item_id"] == bindparam("p4")))
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": workstream_id,
            "p1": now,
            "p2": now_3,
            "p3": version,
            "p4": work_item_id,
        },
    )


def insert_work_item(
    conn,
    work_item_id,
    repo_id,
    title,
    description,
    priority,
    routing_mode,
    requested_worker,
    idempotency_key,
    actor_identity,
    now,
    now_11,
    value_12,
):
    stmt = insert(tables["work_items"]).values(
        {
            "work_item_id": bindparam("p0"),
            "repo_id": bindparam("p1"),
            "title": bindparam("p2"),
            "description": bindparam("p3"),
            "priority": bindparam("p4"),
            "origin_type": sql_constant("web"),
            "routing_mode": bindparam("p5"),
            "requested_worker": bindparam("p6"),
            "creation_idempotency_key": bindparam("p7"),
            "created_by": bindparam("p8"),
            "version": sql_constant(1),
            "created_at": bindparam("p9"),
            "updated_at": bindparam("p10"),
            "metadata_json": bindparam("p11"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": work_item_id,
            "p1": repo_id,
            "p2": title,
            "p3": description,
            "p4": priority,
            "p5": routing_mode,
            "p6": requested_worker,
            "p7": idempotency_key,
            "p8": actor_identity,
            "p9": now,
            "p10": now_11,
            "p11": value_12,
        },
    )


def set_metadata_json_by_work_item_id(conn, value_1, work_item_id):
    stmt = (
        update(tables["work_items"])
        .values({"metadata_json": bindparam("p0")})
        .where((tables["work_items"].c["work_item_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": work_item_id})


def update_for_execute_command(conn, now, now_2, work_item_id):
    stmt = (
        update(tables["work_items"])
        .values(
            {
                "canceled_at": bindparam("p0"),
                "updated_at": bindparam("p1"),
                "version": (tables["work_items"].c["version"] + sql_constant(1)),
            }
        )
        .where((tables["work_items"].c["work_item_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": now, "p1": now_2, "p2": work_item_id})


def update_for_execute_command_2(conn, now, work_item_id):
    stmt = (
        update(tables["work_items"])
        .values(
            {
                "updated_at": bindparam("p0"),
                "version": (tables["work_items"].c["version"] + sql_constant(1)),
            }
        )
        .where((tables["work_items"].c["work_item_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": now, "p1": work_item_id})


def update_for_execute_command_3(conn, now, work_item_id):
    stmt = (
        update(tables["work_items"])
        .values(
            {
                "completed_at": null(),
                "updated_at": bindparam("p0"),
                "version": (tables["work_items"].c["version"] + sql_constant(1)),
            }
        )
        .where((tables["work_items"].c["work_item_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": now, "p1": work_item_id})


def update_for_execute_command_4(conn, now, now_2, work_item_id):
    stmt = (
        update(tables["work_items"])
        .values(
            {
                "activated_at": func.coalesce(tables["work_items"].c["activated_at"], bindparam("p0")),
                "updated_at": bindparam("p1"),
                "version": (tables["work_items"].c["version"] + sql_constant(1)),
            }
        )
        .where((tables["work_items"].c["work_item_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": now, "p1": now_2, "p2": work_item_id})


def update_for_execute_command_5(conn, now, now_2, work_item_id):
    stmt = (
        update(tables["work_items"])
        .values(
            {
                "completed_at": bindparam("p0"),
                "updated_at": bindparam("p1"),
                "version": (tables["work_items"].c["version"] + sql_constant(1)),
            }
        )
        .where((tables["work_items"].c["work_item_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": now, "p1": now_2, "p2": work_item_id})


def set_routing_mode_and_requested_worker_by_work_item_id(conn, selected_mode, selected_worker, work_item_id):
    stmt = (
        update(tables["work_items"])
        .values({"routing_mode": bindparam("p0"), "requested_worker": bindparam("p1")})
        .where((tables["work_items"].c["work_item_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": selected_mode, "p1": selected_worker, "p2": work_item_id})


def update_for_finalize_accepted_result(conn, run_now, run_now_2, value_3):
    stmt = (
        update(tables["work_items"])
        .values(
            {
                "completed_at": bindparam("p0"),
                "updated_at": bindparam("p1"),
                "version": (tables["work_items"].c["version"] + sql_constant(1)),
            }
        )
        .where(
            and_(
                (tables["work_items"].c["work_item_id"] == bindparam("p2")),
                tables["work_items"].c["completed_at"].is_(null()),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": run_now_2, "p2": value_3})


def update_for_complete_published_analysis_items(conn, run_now, run_now_2, work_item_id):
    stmt = (
        update(tables["work_items"])
        .values(
            {
                "completed_at": bindparam("p0"),
                "updated_at": bindparam("p1"),
                "version": (tables["work_items"].c["version"] + sql_constant(1)),
            }
        )
        .where(
            and_(
                tables["work_items"].c["work_item_id"] == bindparam("p2"),
                tables["work_items"].c["completed_at"].is_(null()),
                tables["work_items"].c["canceled_at"].is_(null()),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": run_now_2, "p2": work_item_id})


def update_for_create_task_attempt_and_prompt(conn, run_now, work_item_id):
    stmt = (
        update(tables["work_items"])
        .values(
            {
                "canceled_at": null(),
                "completed_at": null(),
                "updated_at": bindparam("p0"),
                "version": (tables["work_items"].c["version"] + sql_constant(1)),
            }
        )
        .where((tables["work_items"].c["work_item_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": work_item_id})


def update_for_create_task_attempt_and_prompt_2(conn, run_now, work_item_id):
    stmt = (
        update(tables["work_items"])
        .values(
            {
                "completed_at": null(),
                "updated_at": bindparam("p0"),
                "version": (tables["work_items"].c["version"] + sql_constant(1)),
            }
        )
        .where(
            and_(
                tables["work_items"].c["work_item_id"] == bindparam("p1"),
                not_(tables["work_items"].c["completed_at"].is_(null())),
                tables["work_items"].c["canceled_at"].is_(null()),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": work_item_id})


def edit_fields(conn, work_item_id, fields, now):
    allowed = {
        "title",
        "description",
        "priority",
        "requested_worker",
        "routing_mode",
    }
    if not fields or set(fields) - allowed:
        raise ValueError("invalid work item edit fields")
    item = tables["work_items"]
    return statements.write(
        conn,
        update(item)
        .where(item.c.work_item_id == work_item_id)
        .values(**fields, version=item.c.version + 1, updated_at=now),
    )


def mark_remote_terminal(conn, work_item_id, terminal_column, terminal_at, now):
    if terminal_column not in {"completed_at", "canceled_at"}:
        raise ValueError("invalid terminal observation")
    item = tables["work_items"]
    return statements.write(
        conn,
        update(item)
        .where(
            item.c.work_item_id == work_item_id,
            item.c.completed_at.is_(None),
            item.c.canceled_at.is_(None),
        )
        .values(
            {
                terminal_column: terminal_at,
                "updated_at": now,
                "version": item.c.version + 1,
            }
        ),
    )
