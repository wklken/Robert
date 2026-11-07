"""Shared stores operations for execution_migrations."""

from sqlalchemy import bindparam
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_for_apply(conn, value_1, fingerprint, now, value_4, value_5):
    stmt = insert(tables["execution_migrations"]).values(
        {
            "migration_id": bindparam("p0"),
            "source_fingerprint": bindparam("p1"),
            "applied_at": bindparam("p2"),
            "snapshot_json": bindparam("p3"),
            "mappings_json": bindparam("p4"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {"p0": value_1, "p1": fingerprint, "p2": now, "p3": value_4, "p4": value_5},
    )
