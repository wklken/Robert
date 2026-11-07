"""Linked source history and latest attempt evidence."""

from sqlalchemy import or_, select

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables


def linked_workstreams(conn, source_ids):
    streams, links, sources = (
        tables["workstreams"],
        tables["workstream_sources"],
        tables["github_sources"],
    )
    stream = streams.c
    query = (
        select(
            stream.workstream_id,
            stream.lifecycle,
            stream.active_task_id,
            stream.origin_workstream_id,
            stream.created_at,
            stream.updated_at,
            links.c.relationship,
            sources.c.source_key,
        )
        .select_from(
            streams.outerjoin(links, links.c.workstream_id == stream.workstream_id).outerjoin(
                sources,
                sources.c.source_id == links.c.source_id,
            )
        )
        .where(
            or_(
                links.c.source_id.in_(source_ids),
                stream.primary_source_id.in_(source_ids),
            )
        )
        .distinct()
        .order_by(stream.updated_at.desc(), stream.workstream_id.desc())
    )
    return statements.read(conn, query)


def tasks_with_latest_attempt(conn, workstream_ids, limit):
    tasks, attempts = tables["tasks"], tables["attempts"]
    task, attempt = tasks.c, attempts.c
    candidate = attempts.alias("candidate").c
    latest = (
        select(candidate.attempt_id)
        .where(candidate.task_id == task.task_id)
        .order_by(
            candidate.attempt_no.desc(),
            candidate.started_at.desc(),
            candidate.attempt_id.desc(),
        )
        .limit(1)
        .correlate(tasks)
        .scalar_subquery()
    )
    query = (
        select(
            task.created_at,
            task.updated_at,
            task.task_id,
            task.workstream_id,
            task.parent_task_id,
            task.route_id,
            task.expected_output,
            task.lifecycle,
            attempt.attempt_id.label("latest_attempt_id"),
            attempt.status.label("latest_attempt_status"),
            attempt.worktree_path,
            attempt.branch_name,
            attempt.heartbeat_at.label("latest_attempt_heartbeat_at"),
        )
        .select_from(tasks.outerjoin(attempts, attempt.attempt_id == latest))
        .where(
            task.workstream_id.in_(workstream_ids),
        )
        .order_by(task.created_at, task.task_id)
        .limit(limit)
    )
    return statements.read(conn, query)


def recent_events(conn, source_ids, limit):
    event = tables["github_events"].c
    query = (
        select(
            event.event_fingerprint,
            event.event_type,
            event.actor_login,
            event.authorization_status,
            event.event_at,
            event.source_id,
        )
        .where(event.source_id.in_(source_ids))
        .order_by(event.event_at.desc(), event.event_id.desc())
        .limit(limit)
    )
    return statements.read(conn, query)
