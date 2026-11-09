"""Run step history and grouped status counts."""

from sqlalchemy import case, func, select

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries.run_summaries import resolved_summary


def steps(conn, run_ids):
    steps, runs = tables["run_steps"], tables["agent_runs"]
    step = steps.c
    referenced = case((step.step_key.in_(("summarize", "repo_pipelines")), resolved_summary(conn, runs))).label(
        "referenced_summary_json"
    )
    query = (
        select(
            step.run_id,
            step.step_key,
            step.status,
            step.started_at,
            step.finished_at,
            step.output_json,
            step.error_json,
            referenced,
        )
        .select_from(steps.join(runs, runs.c.run_id == step.run_id))
        .where(step.run_id.in_(run_ids))
    )
    return statements.read(conn, query)


def repo_steps(conn, run_ids):
    steps, repos = tables["run_repo_steps"], tables["repos"]
    step = steps.c
    query = (
        select(
            step.run_id,
            step.repo_id,
            repos.c.full_name.label("repo_full_name"),
            step.step_key,
            step.status,
            step.started_at,
            step.finished_at,
            step.output_json,
            step.error_json,
        )
        .select_from(steps.join(repos, repos.c.repo_id == step.repo_id))
        .where(
            step.run_id.in_(run_ids),
        )
        .order_by(repos.c.full_name, step.step_key)
    )
    return statements.read(conn, query)


def step_status_counts(conn, run_ids):
    step = tables["run_steps"].c
    query = (
        select(step.run_id, step.status, func.count().label("count"))
        .where(
            step.run_id.in_(run_ids),
        )
        .group_by(step.run_id, step.status)
    )
    return statements.read(conn, query)
