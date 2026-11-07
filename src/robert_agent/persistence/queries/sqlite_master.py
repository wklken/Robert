"""Shared queries operations for sqlite_master."""

from functools import lru_cache

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, column, and_, bindparam, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def table_names(conn):
    catalog = tables["sqlite_master"]
    query = select(catalog.c.name).where(catalog.c.type == sql_constant("table"))
    return {row[0] for row in statements.read(conn, query)}


def find_for_has_schema(conn):
    query1 = (
        select(column("name"))
        .select_from(tables["sqlite_master"])
        .where(
            and_(
                (column("type") == sql_constant("table")),
                column("name").in_(
                    [
                        sql_constant("schema_migrations"),
                        sql_constant("supervision_checkpoints"),
                        sql_constant("supervision_incidents"),
                        sql_constant("supervision_recovery_actions"),
                    ]
                ),
            )
        )
        .correlate_except(tables["sqlite_master"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


@lru_cache(maxsize=2)
def _exists_statement(tables_only):
    catalog = tables["sqlite_master"]
    query = select(sql_constant(1)).where(catalog.c.name == bindparam("name")).limit(1)
    if tables_only:
        query = query.where(catalog.c.type == sql_constant("table"))
    return query


def table_exists(conn, name):
    return bool(statements.read(conn, _exists_statement(True), {"name": name}))


def object_exists(conn, name):
    return bool(statements.read(conn, _exists_statement(False), {"name": name}))
