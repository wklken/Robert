"""Shared queries operations for projections."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import (
    select,
    literal_column,
    bindparam,
    and_,
    not_,
    func,
    LABEL_STYLE_NONE,
)


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_collect(conn, now, now_2, limit):
    table1 = tables["agent_runs"].alias("ar")
    table3 = tables["leases"].alias("l")
    query4 = (
        select(sql_constant(1))
        .select_from(table3)
        .where(
            and_(
                table3.c["owner_id"] == table1.c["run_id"],
                table3.c["status"] == sql_constant("active"),
                func.julianday(table3.c["expires_at"]) > func.julianday(bindparam("p1")),
            )
        )
        .correlate_except(table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query2 = (
        select(table1.c["run_id"])
        .select_from(table1)
        .where(
            and_(
                table1.c["status"] == sql_constant("running"),
                func.julianday(table1.c["started_at"])
                < func.julianday(bindparam("p0")) - sql_constant(1.0) / sql_constant(24),
                not_(query4.exists()),
            )
        )
        .limit(bindparam("p2"))
        .correlate_except(table1)
        .set_label_style(LABEL_STYLE_NONE)
    )
    sub6 = query2.subquery("sub5")
    query7 = (
        select(func.count(literal_column("*")))
        .select_from(sub6)
        .correlate_except(sub6)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query7, {"p0": now, "p1": now_2, "p2": limit})


def find_for_collect_2(conn, limit):
    query1 = (
        select(tables["notifications"].c["notification_id"])
        .select_from(tables["notifications"])
        .where(
            and_(
                (tables["notifications"].c["status"] == sql_constant("recorded")),
                not_(
                    func.coalesce(
                        func.json_extract(
                            tables["notifications"].c["metadata_json"],
                            sql_constant("$.delivery.status"),
                        ),
                        sql_constant(""),
                    ).in_([sql_constant("sent"), sql_constant("exhausted")])
                ),
            )
        )
        .limit(bindparam("p0"))
        .correlate_except(tables["notifications"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    sub3 = query1.subquery("sub2")
    query4 = (
        select(func.count(literal_column("*")))
        .select_from(sub3)
        .correlate_except(sub3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": limit})


def find_for_transition(conn):
    query1 = select(func.changes()).set_label_style(LABEL_STYLE_NONE)
    return statements.read(conn, query1, {})
