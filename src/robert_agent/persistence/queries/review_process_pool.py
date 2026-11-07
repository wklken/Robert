"""Shared queries operations for review_process_pool."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_status(conn):
    query1 = (
        select(tables["review_process_pool"].c["max_concurrency"])
        .select_from(tables["review_process_pool"])
        .where((tables["review_process_pool"].c["singleton"] == sql_constant(1)))
        .correlate_except(tables["review_process_pool"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})
