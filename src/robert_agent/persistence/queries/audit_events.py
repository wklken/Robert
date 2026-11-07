"""Shared queries operations for audit_events."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, literal_column, func, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_summarize_database(conn):
    query1 = (
        select(func.count(literal_column("*")))
        .select_from(tables["audit_events"])
        .where((tables["audit_events"].c["event_type"] == sql_constant("current_state_closed")))
        .correlate_except(tables["audit_events"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})
