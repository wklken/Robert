"""Workstream ancestry, board rows and operator event history."""

from functools import cache

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import bindparam, or_, select, tuple_, case, func, cast, String

from robert_agent.persistence import statements
from robert_agent.persistence.dialects import sqlite
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


def work_items_with_repositories(
    conn, *, repo=None, include_history=True, workstream_ids=None, attached_only=False, active_only=False, item_ids=None
):
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
    if active_only:
        query = query.where(func.coalesce(items.c.completed_at, "") == "", func.coalesce(items.c.canceled_at, "") == "")
    if item_ids is not None:
        query = query.where(items.c.work_item_id.in_(item_ids))
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


def terminal_board_page(
    conn, *, repo, column, agent, priority, attention, query, before, limit, include_history, attention_types
):
    """Count/filter historical owners in SQL; materialize at most one page of IDs.

    Terminal owners have a fixed column regardless of descendant execution.
    Current task selection retains the same live/active/latest precedence as
    current_task_filtered; only visible page cards need full lifecycle details.
    """
    item, repository = tables["work_items"], tables["repos"]
    stream, task, attempt = tables["workstreams"], tables["tasks"], tables["attempts"]
    live = (
        select(1)
        .where(attempt.c.task_id == task.c.task_id, attempt.c.status.in_(("running", "stale")))
        .correlate(task)
        .exists()
    )
    columns = (
        task.c.task_id,
        task.c.routing_mode,
        task.c.requested_worker,
        task.c.created_at,
        live.label("live"),
        (task.c.task_id == stream.c.active_task_id).label("active"),
    )
    joined = task.join(stream, stream.c.workstream_id == task.c.workstream_id)
    # A task contributes to its own stream and its immediate origin, exactly as
    # related_workstreams does. Rank once, instead of one historical subquery
    # scanning tasks for every work item.
    candidates = (
        select(stream.c.workstream_id.label("owner_id"), *columns)
        .select_from(joined)
        .union_all(
            select(stream.c.origin_workstream_id.label("owner_id"), *columns)
            .select_from(joined)
            .where(stream.c.origin_workstream_id.is_not(None))
        )
        .subquery("board_task_candidates")
    )
    ranked = select(
        *candidates.c,
        func.row_number()
        .over(
            partition_by=candidates.c.owner_id,
            order_by=(
                candidates.c.live.desc(),
                candidates.c.active.desc(),
                candidates.c.created_at.desc(),
                candidates.c.task_id.desc(),
            ),
        )
        .label("rank"),
    ).subquery("ranked_board_tasks")
    current = select(ranked).where(ranked.c.rank == 1).subquery("current_board_task")
    column_value = case((func.coalesce(item.c.canceled_at, "") != "", "history"), else_="done")
    mode = case((current.c.task_id.is_not(None), current.c.routing_mode), else_=item.c.routing_mode)
    worker = case((current.c.task_id.is_not(None), current.c.requested_worker), else_=item.c.requested_worker)
    agent_value = case((mode == "manual", worker), else_="Auto")
    stmt = (
        select(item.c.work_item_id, item.c.updated_at, column_value.label("column"), agent_value.label("agent"))
        .select_from(
            item.join(repository, repository.c.repo_id == item.c.repo_id).outerjoin(
                current, current.c.owner_id == item.c.workstream_id
            )
        )
        .where(or_(func.coalesce(item.c.completed_at, "") != "", func.coalesce(item.c.canceled_at, "") != ""))
    )
    if repo:
        stmt = stmt.where(or_(item.c.repo_id == repo, repository.c.full_name == repo))
    if not include_history:
        stmt = stmt.where(func.coalesce(item.c.canceled_at, "") == "")
    if priority:
        stmt = stmt.where(item.c.priority == priority)
    if agent:
        stmt = stmt.where(agent_value == agent)
    if attention is not None:
        event = tables["work_item_events"]
        response = event.alias("board_attention_response")
        resolved = select(1).where(response.c.resolves_event_id == event.c.event_id).correlate(event).exists()
        unresolved = (
            select(1)
            .where(event.c.work_item_id == item.c.work_item_id, event.c.event_type.in_(attention_types), ~resolved)
            .correlate(item)
            .exists()
        )
        stmt = stmt.where(unresolved if attention in {True, "true", "1", "yes"} else ~unresolved)
    if query:
        branch = (
            select(attempt.c.branch_name)
            .where(attempt.c.task_id == current.c.task_id)
            .order_by(attempt.c.attempt_no.desc())
            .limit(1)
            .correlate(current)
            .scalar_subquery()
        )
        source = tables["github_sources"]
        pr = (
            select(source.c.number)
            .select_from(stream.join(source, source.c.source_id == stream.c.primary_source_id))
            .where(
                or_(
                    stream.c.workstream_id == item.c.workstream_id,
                    stream.c.origin_workstream_id == item.c.workstream_id,
                ),
                source.c.source_type == "pull_request",
            )
            .order_by(stream.c.updated_at.desc(), source.c.number.desc())
            .limit(1)
            .correlate(item)
            .scalar_subquery()
        )
        haystack = (
            func.coalesce(item.c.title, "")
            + " "
            + func.coalesce(item.c.work_item_id, "")
            + " "
            + func.coalesce(current.c.task_id, "")
            + " "
            + func.coalesce(branch, "")
            + " "
            + func.coalesce(cast(pr, String), "")
        )
        # Python lower() defines the existing Unicode search contract. Register
        # it on this read connection; instr treats wildcard characters literally.
        sqlite.register_unicode_lower(conn)
        stmt = stmt.where(func.instr(func.python_lower(haystack), query.lower()) > 0)
    filtered = stmt.subquery("filtered_terminal_items")
    counts = {
        row[0]: row[1]
        for row in statements.read(conn, select(filtered.c.column, func.count()).group_by(filtered.c.column))
    }
    page = select(filtered)
    if before:
        page = page.where(tuple_(filtered.c.updated_at, filtered.c.work_item_id) < tuple_(*before))
    if column:
        page = page.where(filtered.c.column == column)
    agents = [row[0] for row in statements.read(conn, select(filtered.c.agent).distinct())]
    rows = statements.read(
        conn, page.order_by(filtered.c.updated_at.desc(), filtered.c.work_item_id.desc()).limit(limit)
    )
    return {"counts": counts, "rows": rows, "agents": agents}


def canceled_workstream_ids(conn):
    item = tables["work_items"].c
    return [
        row[0]
        for row in statements.read(
            conn,
            select(item.workstream_id).where(
                item.workstream_id.is_not(None), item.canceled_at.is_not(None), item.canceled_at != ""
            ),
        )
    ]
