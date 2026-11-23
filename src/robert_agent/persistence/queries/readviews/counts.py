"""Read-only totals for controller progress and database summaries."""

from hashlib import sha256

from robert_agent.foundation.contracts.delivery import AuditStatus, PublishStatus
from robert_agent.foundation.contracts.work import TaskLifecycle, WorkstreamLifecycle

from sqlalchemy import func, select, true

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables


def _count(table, *criteria):
    return select(func.count()).select_from(table).where(*criteria).scalar_subquery()


def database_totals(conn):
    names = (
        "repos",
        "github_sources",
        "github_events",
        "workstreams",
        "tasks",
        "attempts",
        "worker_results",
        "github_actions",
        "notifications",
        "agent_runs",
    )
    query = select(*(_count(tables[name]).label(name) for name in names))
    return dict(statements.read(conn, query)[0])


def _scoped_progress(rows, identity, repo_id, repo_ids, *states):
    ordered = select(identity, *states).select_from(rows).order_by(identity)
    if repo_ids:
        ordered = ordered.where(repo_id.in_(repo_ids))
    return ordered.subquery()


def _progress_totals(name, rows, **counters):
    # Reading the stable state vector from an ordered subquery preserves identity
    # through same-sized swaps without materializing row records in the loop.
    fingerprint_input = func.group_concat(func.json_array(*rows.c), "").label(name)
    return select(
        fingerprint_input, *(func.count().filter(test).label(key) for key, test in counters.items())
    ).subquery()


def controller_progress(conn, repo_ids=()):
    wakeups, attempts, actions, tasks, streams = (
        tables[name] for name in ("wakeups", "attempts", "github_actions", "tasks", "workstreams")
    )
    repo_id = streams.c.repo_id
    task_scope = tasks.join(streams, tasks.c.workstream_id == streams.c.workstream_id)
    wakeup_states = _scoped_progress(wakeups, wakeups.c.wakeup_id, wakeups.c.repo_id, repo_ids, wakeups.c.status)
    stream_states = _scoped_progress(streams, streams.c.workstream_id, repo_id, repo_ids, streams.c.lifecycle)
    task_states = _scoped_progress(task_scope, tasks.c.task_id, repo_id, repo_ids, tasks.c.lifecycle)
    attempt_states = _scoped_progress(
        task_scope.join(attempts, attempts.c.task_id == tasks.c.task_id),
        attempts.c.attempt_id,
        repo_id,
        repo_ids,
        attempts.c.status,
    )
    action_states = _scoped_progress(
        task_scope.join(actions, actions.c.task_id == tasks.c.task_id),
        actions.c.action_id,
        repo_id,
        repo_ids,
        actions.c.audit_status,
        actions.c.publish_status,
    )
    groups = {
        "wakeups": _progress_totals(
            "wakeups",
            wakeup_states,
            **{f"{status}_wakeups": wakeup_states.c.status == status for status in ("pending", "consumed", "expired")},
        ),
        "workstreams": _progress_totals(
            "workstreams",
            stream_states,
            active_workstreams=stream_states.c.lifecycle == WorkstreamLifecycle.ACTIVE.value,
            waiting_workstreams=stream_states.c.lifecycle == WorkstreamLifecycle.WAITING_FOR_USER.value,
            completed_workstreams=stream_states.c.lifecycle == WorkstreamLifecycle.COMPLETED.value,
        ),
        "tasks": _progress_totals(
            "tasks",
            task_states,
            running_tasks=task_states.c.lifecycle.in_((TaskLifecycle.QUEUED.value, TaskLifecycle.RUNNING.value)),
            completed_tasks=task_states.c.lifecycle == TaskLifecycle.COMPLETED.value,
            failed_tasks=task_states.c.lifecycle == TaskLifecycle.FAILED.value,
        ),
        "attempts": _progress_totals(
            "attempts",
            attempt_states,
            **{
                f"{status}_attempts": attempt_states.c.status == status
                for status in ("prepared", "running", "stale", "completed", "failed")
            },
        ),
        "actions": _progress_totals(
            "actions",
            action_states,
            pending_actions=action_states.c.audit_status == AuditStatus.PENDING.value,
            accepted_unpublished_actions=(action_states.c.audit_status == AuditStatus.ACCEPTED.value)
            & (action_states.c.publish_status == PublishStatus.NOT_PUBLISHED.value),
            published_actions=action_states.c.publish_status == PublishStatus.PUBLISHED.value,
        ),
    }
    source = groups["wakeups"]
    for group in tuple(groups.values())[1:]:
        source = source.join(group, true())
    query = select(*(column for group in groups.values() for column in group.c)).select_from(source)
    totals = dict(statements.read(conn, query)[0])
    state = {name: sha256((totals.pop(name) or "").encode()).digest() for name in groups}
    return totals, state
