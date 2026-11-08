"""Durable scheduling observations for readiness and native round admission."""

from robert_agent.foundation.contracts.execution import BindingStatus, RoundStatus
from robert_agent.foundation.contracts.work import LIVE_TASK_LIFECYCLES, TaskLifecycle

from functools import lru_cache

from robert_agent.persistence.expressions import sql_constant

from typing import Literal

from sqlalchemy import and_, bindparam, func, or_, select

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.predicates import runnable_task
from robert_agent.persistence.queries import sqlite_master

RoundKind = Literal["review", "vote"]

_ROUND_TABLES = {
    "review": tables["review_sessions"],
    "vote": tables["vote_sessions"],
}
_ACTIVE_TASK_STATES = tuple(state.value for state in TaskLifecycle if state.value in LIVE_TASK_LIFECYCLES)


def _queued(rounds):
    return and_(rounds.c.status == sql_constant(RoundStatus.QUEUED.value), rounds.c.task_id.is_(None))


def _unblocked_round(workstream_id, rounds, available_rounds):
    tasks, owner = tables["tasks"], tables["execution_subjects"]
    previous = rounds.alias("previous_round")
    active_task = (
        select(1)
        .select_from(tasks)
        .where(
            tasks.c.workstream_id == workstream_id,
            tasks.c.lifecycle.in_(_ACTIVE_TASK_STATES),
        )
        .correlate_except(tasks)
        .exists()
    )
    owner_needs_attention = (
        select(1)
        .select_from(
            previous.join(
                owner,
                and_(
                    owner.c.subject_type == sql_constant("task"),
                    owner.c.subject_id == previous.c.task_id,
                ),
            )
        )
        .where(
            previous.c.workstream_id == workstream_id,
            owner.c.binding_status == sql_constant(BindingStatus.ATTENTION.value),
        )
        .correlate_except(previous, owner)
        .exists()
    )
    blocking_rounds = []
    for available in available_rounds:
        attended = available.alias()
        blocking_rounds.append(
            select(1)
            .select_from(attended)
            .where(
                attended.c.workstream_id == workstream_id,
                attended.c.status == sql_constant("attention"),
            )
            .correlate_except(attended)
            .exists()
        )
    return ~or_(active_task, owner_needs_attention, *blocking_rounds)


def queued_round_ids(conn, kind: RoundKind, repo_name) -> list[str]:
    rounds = _ROUND_TABLES[kind]
    query = (
        select(rounds.c.round_id)
        .where(
            rounds.c.repo == repo_name,
            _queued(rounds),
        )
        .order_by(rounds.c.created_at, rounds.c.round_id)
    )
    return [round_id for (round_id,) in statements.read(conn, query)]


@lru_cache(maxsize=2)
def _round_admission_statement(kind: RoundKind):
    # Admission has an initialized database with both native round tables.
    return select(1).where(_unblocked_round(bindparam("workstream_id"), _ROUND_TABLES[kind], _ROUND_TABLES.values()))


def native_round_can_prepare(conn, kind: RoundKind, workstream_id) -> bool:
    return bool(statements.read(conn, _round_admission_statement(kind), {"workstream_id": workstream_id}))


@lru_cache(maxsize=8)
def _queued_round_statement(kind: RoundKind, available_kinds):
    rounds, repos = _ROUND_TABLES[kind], tables["repos"]
    available = [_ROUND_TABLES[key] for key in available_kinds]
    return (
        select(1)
        .select_from(rounds.join(repos, repos.c.full_name == rounds.c.repo))
        .where(_queued(rounds), _unblocked_round(rounds.c.workstream_id, rounds, available))
    )


def has_queued_native_round(conn, repo_ids) -> bool:
    available = tuple(kind for kind, table in _ROUND_TABLES.items() if sqlite_master.table_exists(conn, table.name))
    repos = tables["repos"]
    for kind in available:
        query = _queued_round_statement(kind, available)
        if repo_ids:
            query = query.where(repos.c.repo_id.in_(repo_ids))
        if statements.read(conn, query.limit(1)):
            return True
    return False


@lru_cache(maxsize=2)
def _pending_wakeup_statement(include_publication):
    wakeups, tasks = tables["wakeups"], tables["tasks"]
    wakeup = wakeups.c
    kind = func.json_extract(wakeup.metadata_json, sql_constant("$.kind"))
    runnable = (
        select(1)
        .select_from(tasks)
        .where(
            tasks.c.task_id == wakeup.task_id,
            runnable_task(tasks),
        )
        .correlate(wakeups)
        .exists()
    )
    query = (
        select(1)
        .select_from(wakeups)
        .where(
            wakeup.status == "pending",
            or_(
                wakeup.task_id.is_(None),
                and_(
                    wakeup.reason == "manual_operator_request",
                    kind == "github_retrigger",
                ),
                runnable,
            ),
            func.coalesce(kind, sql_constant("")).not_in(
                (
                    sql_constant("github_observation_ready"),
                    sql_constant("github_context_refresh"),
                )
            ),
            wakeup.not_before_at <= bindparam("now"),
            or_(wakeup.expires_at.is_(None), wakeup.expires_at > bindparam("now")),
            wakeup.reason != "worker_result_ready",
        )
    )
    if not include_publication:
        query = query.where(wakeup.reason != "publish_retry_ready")
    return query


def has_pending_local_wakeup(conn, repo_ids, waiting_ids, now, *, include_publication=True) -> bool:
    wakeup = tables["wakeups"].c
    query = _pending_wakeup_statement(include_publication)
    if repo_ids:
        query = query.where(wakeup.repo_id.in_(repo_ids))
    if waiting_ids:
        query = query.where(wakeup.wakeup_id.not_in(waiting_ids))
    return bool(statements.read(conn, query.limit(1), {"now": now}))
