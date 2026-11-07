"""Shared stores operations for project_memory_entries."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, bindparam
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def update_for_upsert_entry(
    conn,
    short_summary,
    long_summary,
    confidence,
    value_4,
    value_5,
    revision_id,
    value_7,
    run_now,
    value_9,
    memory_id,
):
    stmt = (
        update(tables["project_memory_entries"])
        .values(
            {
                "short_summary": bindparam("p0"),
                "long_summary": bindparam("p1"),
                "confidence": bindparam("p2"),
                "source_task_id": bindparam("p3"),
                "source_result_id": bindparam("p4"),
                "current_revision_id": bindparam("p5"),
                "revision_count": bindparam("p6"),
                "updated_at": bindparam("p7"),
                "metadata_json": bindparam("p8"),
            }
        )
        .where((tables["project_memory_entries"].c["memory_id"] == bindparam("p9")))
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": short_summary,
            "p1": long_summary,
            "p2": confidence,
            "p3": value_4,
            "p4": value_5,
            "p5": revision_id,
            "p6": value_7,
            "p7": run_now,
            "p8": value_9,
            "p9": memory_id,
        },
    )


def insert_for_upsert_entry(
    conn,
    memory_id,
    repo_id,
    memory_thread_key,
    kind,
    title,
    short_summary,
    long_summary,
    confidence,
    value_9,
    value_10,
    run_now,
    run_now_12,
    value_13,
):
    stmt = insert(tables["project_memory_entries"]).values(
        {
            "memory_id": bindparam("p0"),
            "repo_id": bindparam("p1"),
            "memory_thread_key": bindparam("p2"),
            "kind": bindparam("p3"),
            "title": bindparam("p4"),
            "short_summary": bindparam("p5"),
            "long_summary": bindparam("p6"),
            "confidence": bindparam("p7"),
            "source_task_id": bindparam("p8"),
            "source_result_id": bindparam("p9"),
            "revision_count": sql_constant(0),
            "created_at": bindparam("p10"),
            "updated_at": bindparam("p11"),
            "metadata_json": bindparam("p12"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": memory_id,
            "p1": repo_id,
            "p2": memory_thread_key,
            "p3": kind,
            "p4": title,
            "p5": short_summary,
            "p6": long_summary,
            "p7": confidence,
            "p8": value_9,
            "p9": value_10,
            "p10": run_now,
            "p11": run_now_12,
            "p12": value_13,
        },
    )
