"""Shared stores operations for runtime_knowledge."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import bindparam
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_for_approve_candidate(
    conn,
    knowledge_id,
    candidate_id,
    repo_id,
    scope_type,
    scope_value,
    title,
    prompt_text,
    value_8,
    approved_by,
    run_now,
    run_now_11,
    run_now_12,
    value_13,
):
    stmt = insert(tables["runtime_knowledge"]).values(
        {
            "knowledge_id": bindparam("p0"),
            "candidate_id": bindparam("p1"),
            "repo_id": bindparam("p2"),
            "scope_type": bindparam("p3"),
            "scope_value": bindparam("p4"),
            "title": bindparam("p5"),
            "prompt_text": bindparam("p6"),
            "retrieval_boost_json": bindparam("p7"),
            "active": sql_constant(1),
            "approved_by": bindparam("p8"),
            "approved_at": bindparam("p9"),
            "created_at": bindparam("p10"),
            "updated_at": bindparam("p11"),
            "metadata_json": bindparam("p12"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": knowledge_id,
            "p1": candidate_id,
            "p2": repo_id,
            "p3": scope_type,
            "p4": scope_value,
            "p5": title,
            "p6": prompt_text,
            "p7": value_8,
            "p8": approved_by,
            "p9": run_now,
            "p10": run_now_11,
            "p11": run_now_12,
            "p12": value_13,
        },
    )
