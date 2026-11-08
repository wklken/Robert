"""Materialized dispatch requests and their delivery facts."""

from sqlalchemy import select, func, and_, or_

from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def get(conn, request_id):
    table = tables["dispatch_requests"]
    rows = statements.read(conn, select(table).where(table.c.request_id == request_id))
    return dict(rows[0]) if rows else None


def list_requests(conn, *, statuses=None, repo_ids=None, identities=None, idle_only=False, limit=50):
    table = tables["dispatch_requests"]
    query = select(table)
    if idle_only:
        stream = tables["workstreams"]
        query = query.where(
            ~select(1)
            .where(
                stream.c.workstream_id == table.c.workstream_id,
                stream.c.lifecycle.in_(["active", "waiting_for_user"]),
                stream.c.active_task_id.is_not(None),
            )
            .exists()
        )
    if statuses is not None:
        query = query.where(table.c.status.in_(statuses))
    if repo_ids is not None:
        query = query.where(table.c.repo_id.in_(repo_ids))
    if identities is not None:
        query = query.where(table.c.identity_json.in_(identities))
    return [
        dict(row) for row in statements.read(conn, query.order_by(table.c.created_at, table.c.request_id).limit(limit))
    ]


def task_binding(request, task):
    """Bind a continuation to the original request, excluding new GitHub requests."""
    root = tables["tasks"].alias("dispatch_root")
    root_lineage = (
        select(func.json_extract(root.c.metadata_json, "$.supervision_lineage_key"))
        .where(root.c.task_id == request.c.task_id)
        .correlate(request)
        .scalar_subquery()
    )
    return and_(
        task.c.workstream_id == request.c.workstream_id,
        or_(
            task.c.task_id == request.c.task_id,
            and_(
                root_lineage.is_not(None),
                func.json_extract(task.c.metadata_json, "$.supervision_lineage_key") == root_lineage,
            ),
        ),
    )


def for_task(conn, task_id):
    request, task = tables["dispatch_requests"], tables["tasks"]
    rows = statements.read(
        conn,
        select(request).select_from(request.join(task, task_binding(request, task))).where(task.c.task_id == task_id),
    )
    return dict(rows[0]) if rows else None


def delivery_candidates(conn, repo_ids):
    request, task, result = (tables[key] for key in ("dispatch_requests", "tasks", "worker_results"))
    eligible = (
        select(1)
        .select_from(task.join(result, result.c.task_id == task.c.task_id))
        .where(
            task_binding(request, task),
            task.c.lifecycle == "completed",
            result.c.output_type.in_(["comment_analysis", "new_pr"]),
        )
        .correlate(request)
        .exists()
    )
    return [
        dict(row)
        for row in statements.read(
            conn,
            select(request)
            .where(request.c.status == "admitted", request.c.repo_id.in_(repo_ids), eligible)
            .order_by(request.c.created_at)
            .limit(50),
        )
    ]


def published_results(conn, request_id):
    request, task, result, action = (
        tables[key] for key in ("dispatch_requests", "tasks", "worker_results", "github_actions")
    )
    query = (
        select(
            result.c.result_id,
            result.c.output_type,
            result.c.handoff,
            task.c.task_id,
            action.c.action_id,
            action.c.action_type,
            action.c.target_url,
            action.c.metadata_json,
        )
        .select_from(
            request.join(task, task_binding(request, task))
            .join(result, result.c.task_id == task.c.task_id)
            .join(action, action.c.result_id == result.c.result_id)
        )
        .where(
            request.c.request_id == request_id,
            task.c.lifecycle == "completed",
            action.c.audit_status == "accepted",
            action.c.publish_status == "published",
        )
    )
    return [dict(row) for row in statements.read(conn, query)]


def latest_task(conn, request_id):
    request, task = tables["dispatch_requests"], tables["tasks"]
    rows = statements.read(
        conn,
        select(task.c.task_id, task.c.lifecycle, task.c.route_id)
        .select_from(request.join(task, task_binding(request, task)))
        .where(request.c.request_id == request_id)
        .order_by(task.c.created_at.desc(), task.c.task_id.desc())
        .limit(1),
    )
    return dict(rows[0]) if rows else None
