"""Shared stores operations for knowledge_candidates."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, bindparam
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def update_for_approve_candidate(conn, run_now, approved_by, candidate_id):
    stmt = (
        update(tables["knowledge_candidates"])
        .values(
            {
                "status": sql_constant("approved"),
                "reviewed_at": bindparam("p0"),
                "reviewer": bindparam("p1"),
            }
        )
        .where((tables["knowledge_candidates"].c["candidate_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": approved_by, "p2": candidate_id})


def update_for_reject_candidate(conn, run_now, reviewer, value_3, candidate_id):
    stmt = (
        update(tables["knowledge_candidates"])
        .values(
            {
                "status": sql_constant("rejected"),
                "reviewed_at": bindparam("p0"),
                "reviewer": bindparam("p1"),
                "review_note": bindparam("p2"),
            }
        )
        .where((tables["knowledge_candidates"].c["candidate_id"] == bindparam("p3")))
    )
    return statements.write(conn, stmt, {"p0": run_now, "p1": reviewer, "p2": value_3, "p3": candidate_id})


def insert_for_propose_candidates(
    conn,
    candidate_id,
    repo_id,
    title,
    short_summary,
    value_5,
    value_6,
    source_json,
    value_8,
    confidence,
    run_now,
    value_11,
):
    stmt = insert(tables["knowledge_candidates"]).values(
        {
            "candidate_id": bindparam("p0"),
            "repo_id": bindparam("p1"),
            "title": bindparam("p2"),
            "summary": bindparam("p3"),
            "prompt_text": bindparam("p4"),
            "candidate_type": bindparam("p5"),
            "source_memory_ids_json": bindparam("p6"),
            "evidence_json": bindparam("p7"),
            "confidence": bindparam("p8"),
            "status": sql_constant("pending"),
            "created_at": bindparam("p9"),
            "metadata_json": bindparam("p10"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": candidate_id,
            "p1": repo_id,
            "p2": title,
            "p3": short_summary,
            "p4": value_5,
            "p5": value_6,
            "p6": source_json,
            "p7": value_8,
            "p8": confidence,
            "p9": run_now,
            "p10": value_11,
        },
    )
