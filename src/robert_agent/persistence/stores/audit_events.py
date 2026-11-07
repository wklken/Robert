"""Shared stores operations for audit_events."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import bindparam, null
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def record_remote_terminal_state_locked(conn, value_1, repo_id, workstream_id, task_id, audit_type, run_now, value_7):
    stmt = insert(tables["audit_events"]).values(
        {
            "audit_id": bindparam("p0"),
            "repo_id": bindparam("p1"),
            "workstream_id": bindparam("p2"),
            "task_id": bindparam("p3"),
            "event_type": bindparam("p4"),
            "created_at": bindparam("p5"),
            "payload_json": bindparam("p6"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": value_1,
            "p1": repo_id,
            "p2": workstream_id,
            "p3": task_id,
            "p4": audit_type,
            "p5": run_now,
            "p6": value_7,
        },
    )


def record_current_state_closed_source(conn, value_1, repo_id, run_now, value_4):
    stmt = insert(tables["audit_events"]).values(
        {
            "audit_id": bindparam("p0"),
            "repo_id": bindparam("p1"),
            "workstream_id": null(),
            "task_id": null(),
            "event_type": sql_constant("current_state_closed"),
            "created_at": bindparam("p2"),
            "payload_json": bindparam("p3"),
        }
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": repo_id, "p2": run_now, "p3": value_4})


def insert_for_audit_transition(conn, value_1, value_2, workstream_id, value_4, now, value_6):
    stmt = insert(tables["audit_events"]).values(
        {
            "audit_id": bindparam("p0"),
            "repo_id": bindparam("p1"),
            "workstream_id": bindparam("p2"),
            "task_id": bindparam("p3"),
            "event_type": sql_constant("pr_remediation_transition"),
            "created_at": bindparam("p4"),
            "payload_json": bindparam("p5"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": value_1,
            "p1": value_2,
            "p2": workstream_id,
            "p3": value_4,
            "p4": now,
            "p5": value_6,
        },
    )
