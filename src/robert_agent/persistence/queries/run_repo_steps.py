"""Shared queries operations for run_repo_steps."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, bindparam, and_, func, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_aggregate_repo_step_status(conn, run_id, step_key):
    query1 = (
        select(tables["run_repo_steps"].c["status"])
        .select_from(tables["run_repo_steps"])
        .where(
            and_(
                (tables["run_repo_steps"].c["run_id"] == bindparam("p0")),
                (tables["run_repo_steps"].c["step_key"] == bindparam("p1")),
            )
        )
        .correlate_except(tables["run_repo_steps"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": run_id, "p1": step_key})


def find_for_running_repo_step(conn, run_id, repo_id):
    query1 = (
        select(tables["run_repo_steps"].c["step_key"])
        .select_from(tables["run_repo_steps"])
        .where(
            and_(
                tables["run_repo_steps"].c["run_id"] == bindparam("p0"),
                tables["run_repo_steps"].c["repo_id"] == bindparam("p1"),
                tables["run_repo_steps"].c["status"] == sql_constant("running"),
            )
        )
        .correlate_except(tables["run_repo_steps"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": run_id, "p1": repo_id})


def mark_repo_failure(conn, run_id, repo_id, step):
    query1 = (
        select(tables["run_repo_steps"].c["status"])
        .select_from(tables["run_repo_steps"])
        .where(
            and_(
                tables["run_repo_steps"].c["run_id"] == bindparam("p0"),
                tables["run_repo_steps"].c["repo_id"] == bindparam("p1"),
                tables["run_repo_steps"].c["step_key"] == bindparam("p2"),
            )
        )
        .correlate_except(tables["run_repo_steps"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": run_id, "p1": repo_id, "p2": step})


def find_for_consumer_completed(conn, repo_id):
    query1 = (
        select(func.max(tables["run_repo_steps"].c["finished_at"]))
        .select_from(tables["run_repo_steps"])
        .where(
            and_(
                tables["run_repo_steps"].c["repo_id"] == bindparam("p0"),
                tables["run_repo_steps"].c["step_key"] == sql_constant("route"),
                tables["run_repo_steps"].c["status"] == sql_constant("succeeded"),
            )
        )
        .correlate_except(tables["run_repo_steps"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id})
