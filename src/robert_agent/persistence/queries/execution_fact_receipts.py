"""Shared queries operations for execution_fact_receipts."""

from sqlalchemy import select, bindparam, and_, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_apply_classified_fact(conn, subject_type, subject_id, fact_id):
    query1 = (
        select(tables["execution_fact_receipts"].c["evidence_json"])
        .select_from(tables["execution_fact_receipts"])
        .where(
            and_(
                tables["execution_fact_receipts"].c["subject_type"] == bindparam("p0"),
                tables["execution_fact_receipts"].c["subject_id"] == bindparam("p1"),
                tables["execution_fact_receipts"].c["fact_id"] == bindparam("p2"),
            )
        )
        .correlate_except(tables["execution_fact_receipts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": subject_type, "p1": subject_id, "p2": fact_id})
