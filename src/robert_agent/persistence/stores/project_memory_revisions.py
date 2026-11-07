"""Shared stores operations for project_memory_revisions."""

from sqlalchemy import bindparam
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_for_upsert_entry(
    conn,
    revision_id,
    memory_id,
    repo_id,
    value_4,
    value_5,
    value_6,
    operation,
    title,
    short_summary,
    long_summary,
    value_11,
    run_now,
):
    stmt = insert(tables["project_memory_revisions"]).values(
        {
            "revision_id": bindparam("p0"),
            "memory_id": bindparam("p1"),
            "repo_id": bindparam("p2"),
            "result_id": bindparam("p3"),
            "task_id": bindparam("p4"),
            "attempt_id": bindparam("p5"),
            "operation": bindparam("p6"),
            "title": bindparam("p7"),
            "short_summary": bindparam("p8"),
            "long_summary": bindparam("p9"),
            "payload_json": bindparam("p10"),
            "created_at": bindparam("p11"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": revision_id,
            "p1": memory_id,
            "p2": repo_id,
            "p3": value_4,
            "p4": value_5,
            "p5": value_6,
            "p6": operation,
            "p7": title,
            "p8": short_summary,
            "p9": long_summary,
            "p10": value_11,
            "p11": run_now,
        },
    )
