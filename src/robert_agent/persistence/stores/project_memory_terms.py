"""Shared stores operations for project_memory_terms."""

from sqlalchemy import delete, bindparam
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def delete_for_replace_terms(conn, memory_id):
    stmt = delete(tables["project_memory_terms"]).where(
        (tables["project_memory_terms"].c["memory_id"] == bindparam("p0"))
    )
    return statements.write(conn, stmt, {"p0": memory_id})


def insert_for_replace_terms(conn, memory_id, repo_id, term_type, term, run_now):
    stmt = (
        insert(tables["project_memory_terms"])
        .values(
            {
                "memory_id": bindparam("p0"),
                "repo_id": bindparam("p1"),
                "term_type": bindparam("p2"),
                "term_value": bindparam("p3"),
                "created_at": bindparam("p4"),
            }
        )
        .prefix_with("OR IGNORE", dialect="sqlite")
    )
    return statements.write(
        conn,
        stmt,
        {"p0": memory_id, "p1": repo_id, "p2": term_type, "p3": term, "p4": run_now},
    )
