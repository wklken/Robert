"""Shared stores operations for project_memory_fts."""

from sqlalchemy import delete, column, bindparam
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def delete_for_sync_fts(conn, memory_id):
    stmt = delete(tables["project_memory_fts"]).where((column("memory_id") == bindparam("p0")))
    return statements.write(conn, stmt, {"p0": memory_id})


def insert_for_sync_fts(
    conn,
    memory_id,
    repo_id,
    title,
    short_summary,
    long_summary,
    value_6,
    value_7,
    value_8,
):
    stmt = insert(tables["project_memory_fts"]).values(
        {
            "memory_id": bindparam("p0"),
            "repo_id": bindparam("p1"),
            "title": bindparam("p2"),
            "short_summary": bindparam("p3"),
            "long_summary": bindparam("p4"),
            "paths": bindparam("p5"),
            "symbols": bindparam("p6"),
            "keywords": bindparam("p7"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": memory_id,
            "p1": repo_id,
            "p2": title,
            "p3": short_summary,
            "p4": long_summary,
            "p5": value_6,
            "p6": value_7,
            "p7": value_8,
        },
    )
