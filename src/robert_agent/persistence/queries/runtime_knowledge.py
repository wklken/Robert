"""Shared queries operations for runtime_knowledge."""

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


def load_runtime_knowledge(conn, repo_id):
    query1 = (
        select(
            tables["runtime_knowledge"].c["knowledge_id"],
            tables["runtime_knowledge"].c["candidate_id"],
            tables["runtime_knowledge"].c["repo_id"],
            tables["runtime_knowledge"].c["scope_type"],
            tables["runtime_knowledge"].c["scope_value"],
            tables["runtime_knowledge"].c["title"],
            tables["runtime_knowledge"].c["prompt_text"],
            tables["runtime_knowledge"].c["retrieval_boost_json"],
            tables["runtime_knowledge"].c["approved_by"],
            tables["runtime_knowledge"].c["approved_at"],
        )
        .select_from(tables["runtime_knowledge"])
        .where(
            and_(
                (tables["runtime_knowledge"].c["repo_id"] == bindparam("p0")),
                (tables["runtime_knowledge"].c["active"] == sql_constant(1)),
            )
        )
        .order_by(
            tables["runtime_knowledge"].c["approved_at"].desc(),
            tables["runtime_knowledge"].c["knowledge_id"].asc(),
        )
        .correlate_except(tables["runtime_knowledge"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id})


def find_for_build_knowledge_review_data(conn, history_limit):
    query1 = (
        select(
            tables["runtime_knowledge"].c["knowledge_id"],
            tables["runtime_knowledge"].c["candidate_id"],
            tables["runtime_knowledge"].c["repo_id"],
            tables["runtime_knowledge"].c["scope_type"],
            tables["runtime_knowledge"].c["scope_value"],
            tables["runtime_knowledge"].c["title"],
            tables["runtime_knowledge"].c["prompt_text"],
            tables["runtime_knowledge"].c["approved_by"],
            tables["runtime_knowledge"].c["approved_at"],
        )
        .select_from(tables["runtime_knowledge"])
        .where((tables["runtime_knowledge"].c["active"] == sql_constant(1)))
        .order_by(
            tables["runtime_knowledge"].c["approved_at"].desc(),
            tables["runtime_knowledge"].c["knowledge_id"].asc(),
        )
        .limit(bindparam("p0"))
        .correlate_except(tables["runtime_knowledge"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": history_limit})


def find_for_build_knowledge_review_data_2(conn):
    query1 = (
        select(func.count(literal_column("*")))
        .select_from(tables["runtime_knowledge"])
        .where((tables["runtime_knowledge"].c["active"] == sql_constant(1)))
        .correlate_except(tables["runtime_knowledge"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})
