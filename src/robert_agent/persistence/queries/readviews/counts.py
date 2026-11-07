"""Read-only totals for controller progress and database summaries."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import func, select

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


def controller_progress(conn):
    wakeups, attempts, actions, tasks, streams = (
        tables[name] for name in ("wakeups", "attempts", "github_actions", "tasks", "workstreams")
    )
    fields = {
        **{
            f"{status}_wakeups": _count(wakeups, wakeups.c.status == status)
            for status in ("pending", "consumed", "expired")
        },
        **{
            f"{status}_attempts": _count(attempts, attempts.c.status == status)
            for status in ("prepared", "running", "stale", "completed", "failed")
        },
        "pending_actions": _count(actions, actions.c.audit_status == sql_constant("pending")),
        "accepted_unpublished_actions": _count(
            actions,
            actions.c.audit_status == sql_constant("accepted"),
            actions.c.publish_status == sql_constant("not_published"),
        ),
        "published_actions": _count(actions, actions.c.publish_status == sql_constant("published")),
        "running_tasks": _count(
            tasks,
            tasks.c.lifecycle.in_((sql_constant("queued"), sql_constant("running"))),
        ),
        "completed_tasks": _count(tasks, tasks.c.lifecycle == sql_constant("completed")),
        "failed_tasks": _count(tasks, tasks.c.lifecycle == sql_constant("failed")),
        "active_workstreams": _count(streams, streams.c.lifecycle == sql_constant("active")),
        "waiting_workstreams": _count(streams, streams.c.lifecycle == sql_constant("waiting_for_user")),
        "completed_workstreams": _count(streams, streams.c.lifecycle == sql_constant("completed")),
    }
    query = select(*(count.label(name) for name, count in fields.items()))
    return dict(statements.read(conn, query)[0])
