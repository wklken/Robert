"""Shared queries operations for github_read_cache."""

from sqlalchemy import select, bindparam, and_, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_get(conn, scope, key):
    query1 = (
        select(
            tables["github_read_cache"].c["version"],
            tables["github_read_cache"].c["payload_json"],
            tables["github_read_cache"].c["expires_at"],
        )
        .select_from(tables["github_read_cache"])
        .where(
            and_(
                (tables["github_read_cache"].c["scope"] == bindparam("p0")),
                (tables["github_read_cache"].c["cache_key"] == bindparam("p1")),
            )
        )
        .correlate_except(tables["github_read_cache"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": scope, "p1": key})
