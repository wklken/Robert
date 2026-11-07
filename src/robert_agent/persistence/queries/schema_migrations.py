"""Shared queries operations for schema_migrations."""

from sqlalchemy import select, bindparam, func, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_database_payloads(conn):
    query1 = (
        select(func.max(tables["schema_migrations"].c["version"]))
        .select_from(tables["schema_migrations"])
        .correlate_except(tables["schema_migrations"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_checksum_by_name(conn, SUPERVISION_MIGRATION_NAME):
    query1 = (
        select(tables["schema_migrations"].c["checksum"])
        .select_from(tables["schema_migrations"])
        .where((tables["schema_migrations"].c["name"] == bindparam("p0")))
        .correlate_except(tables["schema_migrations"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": SUPERVISION_MIGRATION_NAME})
