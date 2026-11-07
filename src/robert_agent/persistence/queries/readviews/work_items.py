"""Workstream ancestry, board rows and operator event history."""

from functools import cache

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import bindparam, or_, select, tuple_

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables


@cache
def _unresolved_attention_statement():
    events = tables["work_item_events"]
    response = events.alias("response")
    has_response = (
        select(1)
        .select_from(response)
        .where(response.c.resolves_event_id == events.c.event_id)
        .correlate(events)
        .exists()
    )
    event = events.c
    query = (
        select(
            event.event_id,
            event.event_type,
            event.body,
            event.created_at,
            event.metadata_json,
        )
        .where(
            event.work_item_id == bindparam("work_item_id"),
            event.event_type.in_(bindparam("event_types", expanding=True)),
            ~has_response,
        )
        .order_by(event.created_at.desc(), event.event_id.desc())
    )
    return query


def unresolved_attention(conn, work_item_id, event_types):
    return statements.read(
        conn,
        _unresolved_attention_statement(),
        {"work_item_id": work_item_id, "event_types": list(event_types)},
    )


def work_items_with_repositories(conn, *, repo=None, include_history=True, workstream_ids=None, attached_only=False):
    if workstream_ids is not None and not workstream_ids:
        return []
    items, repos = tables["work_items"], tables["repos"]
    query = select(items, repos.c.full_name.label("repo_full_name")).select_from(
        items.join(repos, repos.c.repo_id == items.c.repo_id)
    )
    if repo:
        query = query.where(or_(items.c.repo_id == repo, repos.c.full_name == repo))
    if not include_history:
        query = query.where(or_(items.c.canceled_at.is_(None), items.c.canceled_at == sql_constant("")))
    if attached_only:
        query = query.where(items.c.workstream_id.is_not(None))
    else:
        query = query.order_by(items.c.updated_at.desc(), items.c.work_item_id.desc())
    if workstream_ids is not None:
        query = query.where(items.c.workstream_id.in_(workstream_ids))
    return statements.read(conn, query)


def supervision_incidents(conn, work_item_id, task_ids, *, limit=50):
    events, incidents = tables["work_item_events"].c, tables["supervision_incidents"].c
    item_events = select(events.event_id).where(events.work_item_id == work_item_id)
    query = (
        select(
            incidents.incident_id,
            incidents.scope_id,
            incidents.rule_id,
            incidents.status,
            incidents.reason_code,
            incidents.severity,
            incidents.first_observed_at,
            incidents.last_observed_at,
            incidents.next_check_at,
        )
        .where(
            or_(
                (incidents.scope_type == "task") & incidents.scope_id.in_(task_ids),
                (incidents.scope_type == "work_item_event") & incidents.scope_id.in_(item_events),
            )
        )
        .order_by(incidents.last_observed_at.desc())
        .limit(limit)
    )
    return statements.read(conn, query)


def event_page(conn, work_item_id, *, before=None, limit):
    event = tables["work_item_events"].c
    query = select(
        event.event_id,
        event.event_type,
        event.actor_kind,
        event.actor_identity,
        event.body,
        event.resolves_event_id,
        event.idempotency_key,
        event.created_at,
        event.metadata_json,
    ).where(event.work_item_id == work_item_id)
    if before:
        query = query.where(tuple_(event.created_at, event.event_id) < tuple_(*before))
    return statements.read(
        conn,
        query.order_by(event.created_at.desc(), event.event_id.desc()).limit(limit),
    )


def ancestor_workstream_ids(conn, workstream_id):
    streams = tables["workstreams"]
    columns = (streams.c.workstream_id, streams.c.origin_workstream_id)
    ancestors = select(*columns).where(streams.c.workstream_id == workstream_id).cte("ancestors", recursive=True)
    # UNION terminates even if historical links contain a cycle.
    ancestors = ancestors.union(
        select(*columns).select_from(
            streams.join(ancestors, streams.c.workstream_id == ancestors.c.origin_workstream_id)
        )
    )
    return [row[0] for row in statements.read(conn, select(ancestors.c.workstream_id))]


def workstream_sources(conn, workstream_ids):
    streams, repos, sources = (
        tables["workstreams"],
        tables["repos"],
        tables["github_sources"],
    )
    workstream, source = streams.c, sources.c
    query = select(
        workstream.workstream_id,
        workstream.repo_id,
        workstream.primary_source_id,
        workstream.origin_workstream_id,
        workstream.lifecycle.label("workstream_lifecycle"),
        workstream.active_task_id,
        workstream.created_at,
        workstream.updated_at,
        repos.c.full_name.label("repo_full_name"),
        source.source_key,
        source.source_type,
        source.number.label("source_number"),
        source.html_url.label("github_url"),
        source.title.label("source_title"),
        source.state.label("source_state"),
        source.author_login,
        source.source_updated_at,
    ).select_from(
        streams.join(repos, repos.c.repo_id == workstream.repo_id).outerjoin(
            sources,
            source.source_id == workstream.primary_source_id,
        )
    )
    if workstream_ids is not None:
        query = query.where(workstream.workstream_id.in_(workstream_ids))
    return statements.read(conn, query)
