"""Shared queries operations for execution_subjects."""

from sqlalchemy import select, bindparam, and_, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_get_binding(conn, subject_type, subject_id):
    query1 = (
        select(tables["execution_subjects"])
        .select_from(tables["execution_subjects"])
        .where(
            and_(
                (tables["execution_subjects"].c["subject_type"] == bindparam("p0")),
                (tables["execution_subjects"].c["subject_id"] == bindparam("p1")),
            )
        )
        .correlate_except(tables["execution_subjects"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": subject_type, "p1": subject_id})
