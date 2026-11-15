"""Materialized dispatch requests and their delivery facts."""

from sqlalchemy import select, func, and_, or_

from robert_agent.foundation.contracts.delivery import AuditStatus, OutputType
from robert_agent.foundation.contracts.work import RequestEventState, SourceAuthorizationType
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.expressions import valid_json_or_empty
from robert_agent.persistence import statements


def get(conn, request_id):
    table = tables["dispatch_requests"]
    rows = statements.read(conn, select(table).where(table.c.request_id == request_id))
    return dict(rows[0]) if rows else None


def operator_event(conn, event_id):
    event = tables["source_events"]
    rows = statements.read(conn, select(event).where(event.c.event_id == event_id))
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
    request, task, result, ledger, ingress = (
        tables[key] for key in ("dispatch_requests", "tasks", "worker_results", "request_events", "ingress_requests")
    )
    eligible = (
        select(1)
        .select_from(
            task.join(result, result.c.task_id == task.c.task_id)
            .join(ledger, ledger.c.result_id == result.c.result_id)
            .join(ingress, ingress.c.request_id == ledger.c.request_id)
        )
        .where(
            task_binding(request, task),
            task.c.lifecycle == "completed",
            _accepted_final(result),
            ingress.c.request_id == request.c.request_id,
            ingress.c.provider == "github",
            ingress.c.auth_type == SourceAuthorizationType.OPERATOR_DISPATCH.value,
            ledger.c.state == RequestEventState.CONSUMED.value,
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


def _accepted_final(result):
    return and_(
        result.c.output_type.in_((OutputType.COMMENT_ANALYSIS.value, OutputType.NEW_PR.value)),
        func.json_extract(valid_json_or_empty(result.c.metadata_json), "$.audit.status") == AuditStatus.ACCEPTED.value,
    )


def pending_ingress(conn, repo_id, *, after=None, limit=50):
    """Page confirmed historical requests with missing bridge or unsettled accepted result."""
    request, ingress, ledger, task, result = (
        tables[name] for name in ("dispatch_requests", "ingress_requests", "request_events", "tasks", "worker_results")
    )
    eligible_result = (
        select(result.c.result_id)
        .select_from(task.join(result, result.c.task_id == task.c.task_id))
        .where(task_binding(request, task), _accepted_final(result))
        .correlate(request)
        .exists()
    )
    pending = (
        select(ledger.c.event_id)
        .where(ledger.c.request_id == request.c.request_id, ledger.c.state == RequestEventState.PENDING.value)
        .correlate(request)
        .exists()
    )
    return [
        dict(row)
        for row in statements.read(
            conn,
            select(request)
            .outerjoin(ingress, ingress.c.request_id == request.c.request_id)
            .where(
                request.c.repo_id == repo_id,
                request.c.status == "admitted",
                or_(ingress.c.request_id.is_(None), and_(pending, eligible_result)),
                or_(
                    request.c.created_at > after[0],
                    and_(request.c.created_at == after[0], request.c.request_id > after[1]),
                )
                if after
                else True,
            )
            .order_by(request.c.created_at, request.c.request_id)
            .limit(limit),
        )
    ]


def accepted_final_results(conn, request_id):
    """Read at most two results to prove a sole previously accepted final result."""
    request, task, result = (tables[name] for name in ("dispatch_requests", "tasks", "worker_results"))
    return statements.read(
        conn,
        select(result.c.result_id, result.c.task_id, result.c.attempt_id, result.c.consumed_event_fingerprints_json)
        .select_from(request.join(task, task_binding(request, task)).join(result, result.c.task_id == task.c.task_id))
        .where(request.c.request_id == request_id, _accepted_final(result))
        .limit(2),
    )


def ingress_result_matches(conn, *, request_id, source_id, event_id, task_id, attempt_id, result_id):
    """Require a completed current attempt and exact original task/event lineage."""
    request, task, stream, result, attempt, binding = (
        tables[name]
        for name in ("dispatch_requests", "tasks", "workstreams", "worker_results", "attempts", "task_events")
    )
    newer = attempt.alias("newer_dispatch_attempt")
    return bool(
        statements.read(
            conn,
            select(result.c.result_id)
            .select_from(
                request.join(task, task_binding(request, task))
                .join(stream, stream.c.workstream_id == task.c.workstream_id)
                .join(result, result.c.task_id == task.c.task_id)
                .join(attempt, and_(attempt.c.attempt_id == result.c.attempt_id, attempt.c.task_id == task.c.task_id))
                .join(binding, and_(binding.c.task_id == task.c.task_id, binding.c.event_id == event_id))
            )
            .where(
                request.c.request_id == request_id,
                request.c.status == "admitted",
                stream.c.repo_id == request.c.repo_id,
                stream.c.primary_source_id == source_id,
                task.c.task_id == task_id,
                result.c.result_id == result_id,
                result.c.output_type.in_((OutputType.COMMENT_ANALYSIS.value, OutputType.NEW_PR.value)),
                result.c.attempt_id == attempt_id,
                attempt.c.status == "completed",
                ~select(newer.c.attempt_id)
                .where(newer.c.task_id == task.c.task_id, newer.c.attempt_no > attempt.c.attempt_no)
                .correlate(task, attempt)
                .exists(),
            ),
        )
    )


def ingress_recovery_state(conn, repo_id):
    repo = tables["repos"]
    rows = statements.read(
        conn,
        select(func.json_extract(valid_json_or_empty(repo.c.metadata_json), "$.dispatch_ingress_recovery")).where(
            repo.c.repo_id == repo_id
        ),
    )
    return rows[0][0] if rows else None
