"""Shared stores operations for review_process_pool."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, bindparam


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def update_for_configure_process_pool(conn, limit):
    stmt = (
        update(tables["review_process_pool"])
        .values({"max_concurrency": bindparam("p0")})
        .where((tables["review_process_pool"].c["singleton"] == sql_constant(1)))
    )
    return statements.write(conn, stmt, {"p0": limit})
