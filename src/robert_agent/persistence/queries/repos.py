"""Shared queries operations for repos."""

from robert_agent.persistence.expressions import sql_constant, valid_json_or_empty

from sqlalchemy import select, bindparam, and_, cast, func, Integer, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def dispatch_order(conn, repo_ids):
    repo = tables["repos"]
    order = func.json_extract(valid_json_or_empty(repo.c.metadata_json), "$.dispatch_order")
    query = select(repo.c.repo_id, cast(order, Integer)).where(repo.c.repo_id.in_(repo_ids))
    return {repo_id: value or 0 for repo_id, value in statements.read(conn, query)}


def find_for_repo_id(conn):
    query1 = (
        select(tables["repos"].c["repo_id"])
        .select_from(tables["repos"])
        .order_by(tables["repos"].c["repo_id"].asc())
        .limit(sql_constant(1))
        .correlate_except(tables["repos"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_repo_id_by_full_name(conn, full_name):
    query1 = (
        select(tables["repos"].c["repo_id"])
        .select_from(tables["repos"])
        .where((tables["repos"].c["full_name"] == bindparam("p0")))
        .correlate_except(tables["repos"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": full_name})


def list_board(conn):
    query1 = (
        select(tables["repos"].c["repo_id"], tables["repos"].c["full_name"])
        .select_from(tables["repos"])
        .order_by(tables["repos"].c["full_name"].asc())
        .correlate_except(tables["repos"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def load_repos(conn):
    query1 = (
        select(tables["repos"].c["repo_id"], tables["repos"].c["full_name"])
        .select_from(tables["repos"])
        .order_by(tables["repos"].c["full_name"].asc(), tables["repos"].c["repo_id"].asc())
        .correlate_except(tables["repos"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_create_supervision_repair(conn, repo_id):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["repos"])
        .where(
            and_(
                (tables["repos"].c["repo_id"] == bindparam("p0")),
                (tables["repos"].c["enabled"] == sql_constant(1)),
            )
        )
        .correlate_except(tables["repos"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id})


def find_full_name_by_repo_id(conn, repo_id):
    query1 = (
        select(tables["repos"].c["full_name"])
        .select_from(tables["repos"])
        .where((tables["repos"].c["repo_id"] == bindparam("p0")))
        .correlate_except(tables["repos"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id})


def list_ids(conn):
    query1 = (
        select(tables["repos"].c["repo_id"])
        .select_from(tables["repos"])
        .correlate_except(tables["repos"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_save_batch(conn, repo_id):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["repos"])
        .where((tables["repos"].c["repo_id"] == bindparam("p0")))
        .correlate_except(tables["repos"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id})


def find_for_propose(conn, repository):
    query1 = (
        select(tables["repos"].c["repo_id"])
        .select_from(tables["repos"])
        .where(
            and_(
                (func.lower(tables["repos"].c["full_name"]) == bindparam("p0")),
                (tables["repos"].c["enabled"] == sql_constant(1)),
            )
        )
        .correlate_except(tables["repos"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repository})


def full_filtered(conn, items):
    query1 = (
        select(tables["repos"].c["repo_id"])
        .select_from(tables["repos"])
        .where(tables["repos"].c["full_name"].in_(bindparam("items", expanding=True)))
        .correlate_except(tables["repos"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"items": items})
