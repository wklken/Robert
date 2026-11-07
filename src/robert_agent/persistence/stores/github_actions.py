"""Shared stores operations for github_actions."""

from sqlalchemy import literal

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, update, bindparam, and_, func, LABEL_STYLE_NONE
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_canary_fixture(conn, html_url, now, value_3):
    stmt = insert(tables["github_actions"]).values(
        {
            "action_id": sql_constant("action-production-canary"),
            "result_id": sql_constant("result-production-canary"),
            "task_id": sql_constant("task-production-canary"),
            "action_type": sql_constant("comment"),
            "target_url": bindparam("p0"),
            "audit_status": sql_constant("accepted"),
            "publish_status": sql_constant("not_published"),
            "created_at": bindparam("p1"),
            "metadata_json": bindparam("p2"),
        }
    )
    return statements.write(conn, stmt, {"p0": html_url, "p1": now, "p2": value_3})


def mark_skipped(conn, value_1, action_id):
    stmt = (
        update(tables["github_actions"])
        .values(
            {
                "publish_status": sql_constant("skipped"),
                "metadata_json": bindparam("p0"),
            }
        )
        .where((tables["github_actions"].c["action_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": action_id})


def set_metadata_json_by_action_id(conn, value_1, action_id):
    stmt = (
        update(tables["github_actions"])
        .values({"metadata_json": bindparam("p0")})
        .where((tables["github_actions"].c["action_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": action_id})


def mark_published(conn, value_1, value_2, value_3, action_id):
    stmt = (
        update(tables["github_actions"])
        .values(
            {
                "publish_status": sql_constant("published"),
                "external_id": func.coalesce(
                    func.nullif(bindparam("p0"), sql_constant("")),
                    tables["github_actions"].c["external_id"],
                ),
                "target_url": func.coalesce(
                    func.nullif(bindparam("p1"), sql_constant("")),
                    tables["github_actions"].c["target_url"],
                ),
                "metadata_json": bindparam("p2"),
            }
        )
        .where((tables["github_actions"].c["action_id"] == bindparam("p3")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": value_2, "p2": value_3, "p3": action_id})


def record_result_db(
    conn,
    value_1,
    result_id,
    task_id,
    type,
    value_5,
    value_6,
    action_created_at,
    value_8,
):
    stmt = insert(tables["github_actions"]).values(
        {
            "action_id": bindparam("p0"),
            "result_id": bindparam("p1"),
            "task_id": bindparam("p2"),
            "action_type": bindparam("p3"),
            "target_url": bindparam("p4"),
            "external_id": bindparam("p5"),
            "audit_status": sql_constant("pending"),
            "publish_status": sql_constant("not_published"),
            "created_at": bindparam("p6"),
            "metadata_json": bindparam("p7"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": value_1,
            "p1": result_id,
            "p2": task_id,
            "p3": type,
            "p4": value_5,
            "p5": value_6,
            "p6": action_created_at,
            "p7": value_8,
        },
    )


def set_audit_status_by_result_id(conn, status, result_id):
    stmt = (
        update(tables["github_actions"])
        .values({"audit_status": bindparam("p0")})
        .where((tables["github_actions"].c["result_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": status, "p1": result_id})


def insert_fixture(conn, rows):
    stmt = insert(tables["github_actions"]).values(
        {
            "action_id": bindparam("p0"),
            "result_id": bindparam("p1"),
            "task_id": bindparam("p2"),
            "action_type": bindparam("p3"),
            "target_url": bindparam("p4"),
            "audit_status": literal("accepted"),
            "publish_status": literal("not_published"),
            "created_at": bindparam("p5"),
            "metadata_json": bindparam("p6"),
        }
    )
    return statements.write_many(conn, stmt, rows)


def cancel_execution_filtered(conn, items):
    query1 = (
        select(tables["tasks"].c["task_id"])
        .select_from(tables["tasks"])
        .where(tables["tasks"].c["workstream_id"].in_(bindparam("items", expanding=True)))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    stmt = (
        update(tables["github_actions"])
        .values({"publish_status": sql_constant("skipped")})
        .where(
            and_(
                tables["github_actions"].c["task_id"].in_(query1),
                (tables["github_actions"].c["publish_status"] == sql_constant("not_published")),
            )
        )
    )
    return statements.write(conn, stmt, {"items": items})
