"""Completed results and their request context for a calendar period."""

from sqlalchemy import and_, case, func, literal, or_, select

from robert_agent.foundation.contracts.delivery import AuditStatus, OutputType
from robert_agent.foundation.contracts.execution import AttemptStatus
from robert_agent.foundation.contracts.work import TaskLifecycle
from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.expressions import valid_json_or_empty


def _completed_tasks():
    task, attempt, result = (tables[name] for name in ("tasks", "attempts", "worker_results"))
    latest_result = (
        select(result.c.result_id)
        .select_from(
            result.join(
                attempt, (result.c.attempt_id == attempt.c.attempt_id) & (attempt.c.task_id == result.c.task_id)
            )
        )
        .where(
            result.c.task_id == task.c.task_id,
            attempt.c.status == AttemptStatus.COMPLETED.value,
            result.c.output_type.not_in((OutputType.CLASSIFICATION_RESULT.value, OutputType.WAITING_FOR_USER.value)),
            func.json_extract(valid_json_or_empty(result.c.metadata_json), "$.audit.status")
            == AuditStatus.ACCEPTED.value,
        )
        .order_by(attempt.c.attempt_no.desc(), result.c.result_id.desc())
        .correlate(task)
        .limit(1)
        .scalar_subquery()
    )
    return (
        select(
            task.c.task_id,
            task.c.workstream_id,
            task.c.updated_at.label("task_updated_at"),
            result.c.result_id,
            result.c.output_type,
            result.c.attempt_id,
        )
        .select_from(task.join(result, result.c.result_id == latest_result))
        .where(task.c.lifecycle == TaskLifecycle.COMPLETED.value)
    )


def _request_owners(tasks):
    stream, item = tables["workstreams"], tables["work_items"]
    stream_refs = (stream.c.workstream_id, stream.c.repo_id, stream.c.origin_workstream_id, stream.c.primary_source_id)
    # Each derived stream inherits the nearest work item in its repository.
    # Older streams may lack a work item, so use the earliest available source.
    # Segments are encoded before delimiting to handle arbitrary stored IDs.
    segment = func.hex(stream.c.workstream_id) + literal("/")
    ancestors = (
        select(
            stream.c.workstream_id.label("requested_stream_id"),
            *stream_refs,
            literal(0).label("distance"),
            (literal("/") + segment).label("visited"),
        )
        .select_from(stream)
        .where(stream.c.workstream_id.in_(select(tasks.c.workstream_id)))
        .cte("timeline_ancestors", recursive=True)
    )
    ancestors = ancestors.union_all(
        select(
            ancestors.c.requested_stream_id,
            *stream_refs,
            ancestors.c.distance + 1,
            ancestors.c.visited + segment,
        )
        .select_from(stream.join(ancestors, stream.c.workstream_id == ancestors.c.origin_workstream_id))
        .where(
            stream.c.repo_id == ancestors.c.repo_id,
            func.instr(ancestors.c.visited, literal("/") + segment) == 0,
        )
    )
    has_item = item.c.work_item_id.is_not(None)
    has_source = ancestors.c.primary_source_id.is_not(None)
    return (
        select(
            ancestors.c.requested_stream_id,
            ancestors.c.repo_id,
            item.c.work_item_id,
            item.c.title.label("work_item_title"),
            func.coalesce(item.c.origin_source_id, ancestors.c.primary_source_id).label("request_source_id"),
            func.row_number()
            .over(
                partition_by=ancestors.c.requested_stream_id,
                order_by=(
                    case((has_item, 0), (has_source, 1), else_=2),
                    case((has_item, ancestors.c.distance), else_=-ancestors.c.distance),
                ),
            )
            .label("rank"),
        )
        .select_from(ancestors.outerjoin(item, item.c.workstream_id == ancestors.c.workstream_id))
        .subquery("timeline_request_owners")
    )


def completed_results(conn, earliest_date, latest_date):
    tasks = (
        _completed_tasks()
        .where(tables["tasks"].c.updated_at >= earliest_date, tables["worker_results"].c.created_at < latest_date)
        .cte("completed_tasks")
    )
    owners = _request_owners(tasks)
    repo, source, event = (tables[name] for name in ("repos", "github_sources", "work_item_events"))
    context = (
        tasks.join(owners, (owners.c.requested_stream_id == tasks.c.workstream_id) & (owners.c.rank == 1))
        .join(repo, repo.c.repo_id == owners.c.repo_id)
        .outerjoin(source, source.c.source_id == owners.c.request_source_id)
        .outerjoin(
            event,
            (event.c.work_item_id == owners.c.work_item_id)
            & (event.c.idempotency_key == ("result:" + tasks.c.result_id))
            & (event.c.event_type == "result_recorded"),
        )
    )
    query = select(
        tasks.c.task_id,
        tasks.c.task_updated_at,
        event.c.created_at.label("local_result_at"),
        tasks.c.result_id,
        tasks.c.output_type,
        repo.c.full_name.label("repository"),
        owners.c.work_item_id,
        owners.c.work_item_title,
        source.c.title.label("request_title"),
        source.c.html_url.label("request_url"),
        source.c.project_key.label("request_project"),
    ).select_from(context)
    return statements.read(conn, query)


def completed_result_receipt(conn, task_id, result_id):
    task, result, attempt = (tables[name] for name in ("tasks", "worker_results", "attempts"))
    selected = _completed_tasks().where(task.c.task_id == task_id, result.c.result_id == result_id).subquery()
    dispatch = valid_json_or_empty(attempt.c.metadata_json)
    query = select(
        selected.c.output_type,
        func.json_extract(valid_json_or_empty(result.c.metadata_json), "$.normalized_payload.receipt_summary").label(
            "receipt_summary"
        ),
        result.c.verification_json,
        result.c.created_at.label("result_created_at"),
        attempt.c.attempt_no,
        attempt.c.started_at,
        attempt.c.finished_at,
        func.json_extract(dispatch, "$.dispatch.worker_name").label("worker"),
        func.json_extract(dispatch, "$.dispatch.model").label("model"),
    ).select_from(
        selected.join(result, result.c.result_id == selected.c.result_id).join(
            attempt, attempt.c.attempt_id == selected.c.attempt_id
        )
    )
    return statements.read(conn, query)


def receipt_attempts(conn, task_id, attempt_no, result_created_at):
    task, stream, attempt = (tables[name] for name in ("tasks", "workstreams", "attempts"))
    lineage = func.json_extract(valid_json_or_empty(task.c.metadata_json), "$.supervision_lineage_key")
    anchor = (
        select(stream.c.repo_id, func.nullif(lineage, "").label("lineage"))
        .select_from(task.join(stream, stream.c.workstream_id == task.c.workstream_id))
        .where(task.c.task_id == task_id)
        .subquery()
    )
    cutoff = func.julianday(result_created_at)
    return statements.read(
        conn,
        select(
            attempt.c.task_id,
            attempt.c.attempt_id,
            attempt.c.started_at,
            attempt.c.finished_at,
            attempt.c.metadata_json,
            anchor.c.lineage,
        )
        .select_from(
            attempt.join(task, task.c.task_id == attempt.c.task_id)
            .join(stream, stream.c.workstream_id == task.c.workstream_id)
            .join(anchor, anchor.c.repo_id == stream.c.repo_id)
        )
        .where(
            or_(
                and_(task.c.task_id == task_id, attempt.c.attempt_no <= attempt_no),
                and_(
                    task.c.task_id != task_id,
                    lineage == anchor.c.lineage,
                    func.julianday(task.c.created_at) <= cutoff,
                    or_(attempt.c.started_at.is_(None), func.julianday(attempt.c.started_at) <= cutoff),
                ),
            )
        ),
    )
