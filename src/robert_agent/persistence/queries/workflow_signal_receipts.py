"""Shared queries operations for workflow_signal_receipts."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, bindparam, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_workflow_id_and_topic_by_signal_id(conn, signal):
    query1 = (
        select(
            tables["workflow_signal_receipts"].c["workflow_id"],
            tables["workflow_signal_receipts"].c["topic"],
        )
        .select_from(tables["workflow_signal_receipts"])
        .where((tables["workflow_signal_receipts"].c["signal_id"] == bindparam("p0")))
        .correlate_except(tables["workflow_signal_receipts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": signal})


def find_for_migration_coverage(conn, signal_id):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["workflow_signal_receipts"])
        .where((tables["workflow_signal_receipts"].c["signal_id"] == bindparam("p0")))
        .correlate_except(tables["workflow_signal_receipts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": signal_id})
