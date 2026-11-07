"""Shared queries operations for workflow_outbox."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import (
    select,
    literal_column,
    bindparam,
    func,
    LABEL_STYLE_NONE,
)


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_require_migrated(conn):
    query1 = (
        select(
            tables["workflow_outbox"].c["signal_id"],
            tables["workflow_outbox"].c["workflow_id"],
            tables["workflow_outbox"].c["topic"],
        )
        .select_from(tables["workflow_outbox"])
        .correlate_except(tables["workflow_outbox"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_collect(conn, now):
    query1 = (
        select(
            func.count(literal_column("*")),
            func.max(
                sql_constant(0),
                (
                    (
                        func.julianday(bindparam("p0"))
                        - func.min(func.julianday(tables["workflow_outbox"].c["created_at"]))
                    )
                    * sql_constant(86400)
                ),
            ),
        )
        .select_from(tables["workflow_outbox"])
        .where((tables["workflow_outbox"].c["status"] == sql_constant("pending")))
        .correlate_except(tables["workflow_outbox"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": now})


def find_for_collect_2(conn):
    query1 = (
        select(func.count(literal_column("*")))
        .select_from(tables["workflow_outbox"])
        .where((tables["workflow_outbox"].c["status"] == sql_constant("attention")))
        .correlate_except(tables["workflow_outbox"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})
