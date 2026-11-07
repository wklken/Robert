"""Shared queries operations for execution_migrations."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, bindparam, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_migration_coverage(conn):
    query1 = (
        select(
            tables["execution_migrations"].c["snapshot_json"],
            tables["execution_migrations"].c["mappings_json"],
        )
        .select_from(tables["execution_migrations"])
        .correlate_except(tables["execution_migrations"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_apply(conn, value_1):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["execution_migrations"])
        .where((tables["execution_migrations"].c["source_fingerprint"] == bindparam("p0")))
        .correlate_except(tables["execution_migrations"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": value_1})
