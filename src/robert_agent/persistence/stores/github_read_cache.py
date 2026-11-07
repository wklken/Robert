"""Shared stores operations for github_read_cache."""

from sqlalchemy import delete, bindparam
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_for_put(conn, scope, key, version, value_4, value_5):
    stmt = insert(tables["github_read_cache"]).values(
        {
            "scope": bindparam("p0"),
            "cache_key": bindparam("p1"),
            "version": bindparam("p2"),
            "payload_json": bindparam("p3"),
            "expires_at": bindparam("p4"),
        }
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["scope", "cache_key"],
        set_={
            "version": stmt.excluded["version"],
            "payload_json": stmt.excluded["payload_json"],
            "expires_at": stmt.excluded["expires_at"],
        },
    )
    return statements.write(
        conn,
        stmt,
        {"p0": scope, "p1": key, "p2": version, "p3": value_4, "p4": value_5},
    )


def delete_for_put(conn, now):
    stmt = delete(tables["github_read_cache"]).where((tables["github_read_cache"].c["expires_at"] < bindparam("p0")))
    return statements.write(conn, stmt, {"p0": now})
