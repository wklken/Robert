"""Legacy result contract evidence used by the cutover guard."""

from robert_agent.foundation.contracts.delivery import AuditStatus, PublishStatus
from robert_agent.foundation.contracts.execution import AttemptStatus
from robert_agent.foundation.contracts.work import TaskLifecycle, WorkstreamLifecycle

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, and_, or_, func


from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.dialects.sqlite import table_info


def legacy_evidence(conn, contract_id):
    tasks, attempts = tables["tasks"], tables["attempts"]
    results, streams, actions = (
        tables["worker_results"],
        tables["workstreams"],
        tables["github_actions"],
    )
    task, attempt, result = tasks.c, attempts.c, results.c
    unfinished = task.lifecycle.in_(
        (sql_constant(TaskLifecycle.QUEUED.value), sql_constant(TaskLifecycle.RUNNING.value))
    )

    attempt_query = (
        select(
            attempt.attempt_id,
            attempt.task_id,
            attempt.status,
            attempt.metadata_json,
            task.lifecycle,
        )
        .select_from(attempts.join(tasks, task.task_id == attempt.task_id))
        .where(
            or_(
                attempt.status.in_(
                    (sql_constant(AttemptStatus.RUNNING.value), sql_constant(AttemptStatus.STALE.value))
                ),
                and_(attempt.status == AttemptStatus.PREPARED, unfinished),
            ),
            func.coalesce(
                func.json_extract(attempt.metadata_json, sql_constant("$.result_contract")),
                sql_constant(""),
            )
            != contract_id,
        )
    )

    # The uncorrelated sets avoid rescanning the terminal history for each result.
    active_tasks = select(streams.c.active_task_id).where(
        streams.c.lifecycle == sql_constant(WorkstreamLifecycle.ACTIVE.value)
    )
    pending_results = select(actions.c.result_id).where(
        actions.c.publish_status == sql_constant(PublishStatus.NOT_PUBLISHED.value),
        actions.c.audit_status.in_((sql_constant(AuditStatus.PENDING.value), sql_constant(AuditStatus.ACCEPTED.value))),
    )
    result_query = (
        select(result.result_id, result.task_id)
        .select_from(results.join(tasks, task.task_id == result.task_id))
        .where(
            func.coalesce(
                func.json_extract(result.metadata_json, sql_constant("$.result_contract")),
                sql_constant(""),
            )
            != contract_id,
            or_(
                unfinished,
                task.task_id.in_(active_tasks),
                result.result_id.in_(pending_results),
            ),
        )
    )

    # Databases from before task metadata still need the legacy startup check.
    if any(column[1] == "metadata_json" for column in table_info(conn, "tasks")):
        unisolated = func.coalesce(
            func.json_extract(task.metadata_json, sql_constant("$.execution_attention.reason_code")),
            sql_constant(""),
        ) != ("result_contract_cutover")
        attempt_query = attempt_query.where(unisolated)
        result_query = result_query.where(unisolated)
    return statements.read(conn, attempt_query), statements.read(conn, result_query)
