"""Shared queries operations for knowledge_candidates."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import (
    select,
    literal_column,
    bindparam,
    and_,
    func,
    LABEL_STYLE_NONE,
)


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_approve_candidate(conn, candidate_id):
    query1 = (
        select(
            tables["knowledge_candidates"].c["repo_id"],
            tables["knowledge_candidates"].c["title"],
            tables["knowledge_candidates"].c["prompt_text"],
            tables["knowledge_candidates"].c["metadata_json"],
            tables["knowledge_candidates"].c["status"],
        )
        .select_from(tables["knowledge_candidates"])
        .where((tables["knowledge_candidates"].c["candidate_id"] == bindparam("p0")))
        .correlate_except(tables["knowledge_candidates"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": candidate_id})


def find_status_by_candidate_id(conn, candidate_id):
    query1 = (
        select(tables["knowledge_candidates"].c["status"])
        .select_from(tables["knowledge_candidates"])
        .where((tables["knowledge_candidates"].c["candidate_id"] == bindparam("p0")))
        .correlate_except(tables["knowledge_candidates"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": candidate_id})


def find_for_show_candidate(conn, candidate_id):
    query1 = (
        select(
            tables["knowledge_candidates"].c["candidate_id"],
            tables["knowledge_candidates"].c["repo_id"],
            tables["knowledge_candidates"].c["title"],
            tables["knowledge_candidates"].c["summary"],
            tables["knowledge_candidates"].c["prompt_text"],
            tables["knowledge_candidates"].c["candidate_type"],
            tables["knowledge_candidates"].c["source_memory_ids_json"],
            tables["knowledge_candidates"].c["evidence_json"],
            tables["knowledge_candidates"].c["confidence"],
            tables["knowledge_candidates"].c["status"],
            tables["knowledge_candidates"].c["created_at"],
            tables["knowledge_candidates"].c["reviewed_at"],
            tables["knowledge_candidates"].c["reviewer"],
            tables["knowledge_candidates"].c["review_note"],
            tables["knowledge_candidates"].c["metadata_json"],
        )
        .select_from(tables["knowledge_candidates"])
        .where((tables["knowledge_candidates"].c["candidate_id"] == bindparam("p0")))
        .correlate_except(tables["knowledge_candidates"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": candidate_id})


def find_for_propose_candidates(conn, repo_id, title, source_json):
    query1 = (
        select(tables["knowledge_candidates"].c["candidate_id"])
        .select_from(tables["knowledge_candidates"])
        .where(
            and_(
                tables["knowledge_candidates"].c["repo_id"] == bindparam("p0"),
                tables["knowledge_candidates"].c["title"] == bindparam("p1"),
                tables["knowledge_candidates"].c["source_memory_ids_json"] == bindparam("p2"),
            )
        )
        .correlate_except(tables["knowledge_candidates"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id, "p1": title, "p2": source_json})


def find_for_build_knowledge_review_data(conn):
    query1 = (
        select(
            tables["knowledge_candidates"].c["status"],
            func.count(literal_column("*")).label("count"),
        )
        .select_from(tables["knowledge_candidates"])
        .group_by(tables["knowledge_candidates"].c["status"])
        .correlate_except(tables["knowledge_candidates"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_build_knowledge_review_data_2(conn, history_limit):
    query1 = (
        select(tables["knowledge_candidates"].c["candidate_id"])
        .select_from(tables["knowledge_candidates"])
        .where((tables["knowledge_candidates"].c["status"] == sql_constant("pending")))
        .order_by(
            tables["knowledge_candidates"].c["created_at"].desc(),
            tables["knowledge_candidates"].c["candidate_id"].asc(),
        )
        .limit(bindparam("p0"))
        .correlate_except(tables["knowledge_candidates"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": history_limit})


def list_candidates(conn, repo_id=None, status=None):
    query1 = (
        select(
            tables["knowledge_candidates"].c["candidate_id"],
            tables["knowledge_candidates"].c["repo_id"],
            tables["knowledge_candidates"].c["title"],
            tables["knowledge_candidates"].c["summary"],
            tables["knowledge_candidates"].c["candidate_type"],
            tables["knowledge_candidates"].c["confidence"],
            tables["knowledge_candidates"].c["status"],
            tables["knowledge_candidates"].c["created_at"],
            tables["knowledge_candidates"].c["reviewed_at"],
            tables["knowledge_candidates"].c["reviewer"],
        )
        .select_from(tables["knowledge_candidates"])
        .order_by(
            tables["knowledge_candidates"].c["created_at"].desc(),
            tables["knowledge_candidates"].c["candidate_id"].asc(),
        )
        .correlate_except(tables["knowledge_candidates"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    if repo_id:
        query1 = query1.where(tables["knowledge_candidates"].c.repo_id == repo_id)
    if status:
        query1 = query1.where(tables["knowledge_candidates"].c.status == status)
    return statements.read(conn, query1, {})
