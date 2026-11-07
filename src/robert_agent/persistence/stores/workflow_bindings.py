"""Shared stores operations for workflow_bindings."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, bindparam, and_


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def update_for_apply(conn, now, now_2, subject_type, subject_id):
    stmt = (
        update(tables["workflow_bindings"])
        .values(
            {
                "binding_status": sql_constant("terminal"),
                "terminal_at": bindparam("p0"),
                "updated_at": bindparam("p1"),
            }
        )
        .where(
            and_(
                (tables["workflow_bindings"].c["subject_type"] == bindparam("p2")),
                (tables["workflow_bindings"].c["subject_id"] == bindparam("p3")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": now_2, "p2": subject_type, "p3": subject_id})
