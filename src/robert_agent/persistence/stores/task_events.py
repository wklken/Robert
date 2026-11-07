"""Shared stores operations for task_events."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, update, bindparam, and_, LABEL_STYLE_NONE
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_for_append_event_to_active_task(conn, task_id, value_2, relationship, run_now):
    stmt = (
        insert(tables["task_events"])
        .values(
            {
                "task_id": bindparam("p0"),
                "event_id": bindparam("p1"),
                "relationship": bindparam("p2"),
                "created_at": bindparam("p3"),
            }
        )
        .prefix_with("OR IGNORE", dialect="sqlite")
    )
    return statements.write(conn, stmt, {"p0": task_id, "p1": value_2, "p2": relationship, "p3": run_now})


def mark_consumed_events(conn, task_id, fingerprint):
    query1 = (
        select(tables["github_events"].c["event_id"])
        .select_from(tables["github_events"])
        .where((tables["github_events"].c["event_fingerprint"] == bindparam("p1")))
        .correlate_except(tables["github_events"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    stmt = (
        update(tables["task_events"])
        .values({"relationship": sql_constant("consumed")})
        .where(
            and_(
                (tables["task_events"].c["task_id"] == bindparam("p0")),
                tables["task_events"].c["event_id"].in_(query1),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": task_id, "p1": fingerprint})


def insert_for_resume_waiting_vote(conn, value_1, value_2, run_now):
    stmt = (
        insert(tables["task_events"])
        .values(
            {
                "task_id": bindparam("p0"),
                "event_id": bindparam("p1"),
                "relationship": sql_constant("consumed"),
                "created_at": bindparam("p2"),
            }
        )
        .prefix_with("OR IGNORE", dialect="sqlite")
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": value_2, "p2": run_now})


def insert_for_create_task_attempt_and_prompt(conn, task_id, value_2, relationship, run_now):
    stmt = insert(tables["task_events"]).values(
        {
            "task_id": bindparam("p0"),
            "event_id": bindparam("p1"),
            "relationship": bindparam("p2"),
            "created_at": bindparam("p3"),
        }
    )
    return statements.write(conn, stmt, {"p0": task_id, "p1": value_2, "p2": relationship, "p3": run_now})
