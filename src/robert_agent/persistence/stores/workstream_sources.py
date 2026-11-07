"""Shared stores operations for workstream_sources."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import bindparam
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_canary_fixture(conn, source_key, now):
    stmt = insert(tables["workstream_sources"]).values(
        {
            "workstream_id": bindparam("p0"),
            "source_id": sql_constant("source-production-canary"),
            "relationship": sql_constant("primary"),
            "created_at": bindparam("p1"),
        }
    )
    return statements.write(conn, stmt, {"p0": source_key, "p1": now})


def insert_for_materialize_open_pr_workstream(conn, pr_source_key, pr_source_id, run_now):
    stmt = insert(tables["workstream_sources"]).values(
        {
            "workstream_id": bindparam("p0"),
            "source_id": bindparam("p1"),
            "relationship": sql_constant("derived_pr"),
            "created_at": bindparam("p2"),
        }
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["workstream_id", "source_id"])
    return statements.write(conn, stmt, {"p0": pr_source_key, "p1": pr_source_id, "p2": run_now})


def insert_for_create_task_attempt_and_prompt(conn, workstream_id, related_source_id, value_3, run_now):
    stmt = insert(tables["workstream_sources"]).values(
        {
            "workstream_id": bindparam("p0"),
            "source_id": bindparam("p1"),
            "relationship": bindparam("p2"),
            "created_at": bindparam("p3"),
        }
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["workstream_id", "source_id"])
    return statements.write(
        conn,
        stmt,
        {"p0": workstream_id, "p1": related_source_id, "p2": value_3, "p3": run_now},
    )
