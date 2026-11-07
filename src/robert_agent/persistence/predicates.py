"""Shared persisted ownership predicates used by queries and atomic writes."""

from robert_agent.foundation.contracts.delivery import AuditStatus, PublicationStatus, PublishStatus
from robert_agent.foundation.contracts.execution import AttemptStatus, BindingStatus

from robert_agent.persistence.expressions import sql_constant

from robert_agent.persistence.catalog import tables

from sqlalchemy import and_, func, or_, select

from robert_agent.models.execution import ExecutionSubject


def occupied_attempt(attempt):
    """A result does not establish that its recorded process has exited."""
    return or_(
        attempt.c.status.in_((sql_constant(AttemptStatus.RUNNING.value), sql_constant(AttemptStatus.STALE.value))),
        and_(
            attempt.c.status == sql_constant(AttemptStatus.COMPLETED.value),
            func.json_type(attempt.c.metadata_json, sql_constant("$.dispatch.pid")) == sql_constant("integer"),
            func.json_extract(attempt.c.metadata_json, sql_constant("$.process_stopped_at")).is_(None),
        ),
    )


def execution_attention_free(task):
    control = ExecutionSubject.__table__.alias("execution_control")
    stopped = (
        select(1)
        .select_from(control)
        .where(
            control.c.binding_status == sql_constant(BindingStatus.ATTENTION.value),
            or_(
                and_(
                    control.c.subject_type == sql_constant("task"),
                    control.c.subject_id == task.c.task_id,
                ),
                and_(
                    control.c.subject_type == sql_constant("remediation_episode"),
                    control.c.subject_id
                    == func.json_extract(task.c.metadata_json, sql_constant("$.remediation_episode_id")),
                ),
            ),
        )
        .correlate_except(control)
        .exists()
    )
    return and_(
        func.json_extract(task.c.metadata_json, sql_constant("$.execution_attention")).is_(None),
        ~stopped,
    )


def runnable_task(task):
    return and_(
        execution_attention_free(task),
        func.json_extract(task.c.metadata_json, sql_constant("$.audit_retry_required")).is_(None),
    )


def earlier_action(earlier, action):
    return and_(
        earlier.c.result_id == action.c.result_id,
        earlier.c.publish_status == sql_constant(PublishStatus.NOT_PUBLISHED.value),
        or_(
            earlier.c.created_at < action.c.created_at,
            and_(
                earlier.c.created_at == action.c.created_at,
                earlier.c.action_id < action.c.action_id,
            ),
        ),
    )


def publication_ready(action, now):
    """Same eligibility before paging, recovery checks, and scheduler probes."""
    process = tables["attempts"].alias("publication_process")
    result = tables["worker_results"].alias("publication_result")
    intent = tables["publication_intents"].alias("publication_intent")
    earlier = tables["github_actions"].alias("earlier_action")
    earlier_intent = tables["publication_intents"].alias("earlier_intent")
    running = (
        select(1)
        .select_from(process.join(result, result.c.attempt_id == process.c.attempt_id))
        .where(
            result.c.result_id == action.c.result_id,
            func.json_type(process.c.metadata_json, sql_constant("$.dispatch.pid")) == sql_constant("integer"),
            func.json_extract(process.c.metadata_json, sql_constant("$.process_stopped_at")).is_(None),
        )
        .correlate_except(process, result)
        .exists()
    )
    executing = (
        select(1)
        .where(
            intent.c.action_id == action.c.action_id,
            intent.c.status == sql_constant(PublicationStatus.EXECUTING.value),
        )
        .correlate_except(intent)
        .exists()
    )
    attention = (
        select(1)
        .where(
            intent.c.action_id == action.c.action_id,
            intent.c.status == sql_constant(PublicationStatus.ATTENTION.value),
        )
        .correlate_except(intent)
        .exists()
    )
    prior_attention = (
        select(1)
        .where(
            earlier_intent.c.action_id == earlier.c.action_id,
            earlier_intent.c.status == sql_constant(PublicationStatus.ATTENTION.value),
        )
        .correlate_except(earlier_intent)
        .exists()
    )
    blocked = (
        select(1)
        .where(
            earlier_action(earlier, action),
            earlier.c.audit_status.in_(
                (sql_constant(AuditStatus.PENDING.value), sql_constant(AuditStatus.ACCEPTED.value))
            ),
            or_(
                earlier.c.audit_status == sql_constant(AuditStatus.PENDING.value),
                func.json_extract(earlier.c.metadata_json, sql_constant("$.publish.status"))
                == sql_constant("publish_failed"),
                prior_attention,
            ),
        )
        .correlate_except(earlier)
        .exists()
    )
    retry_at = func.json_extract(action.c.metadata_json, sql_constant("$.publish.next_retry_at"))
    return and_(
        action.c.audit_status == sql_constant(AuditStatus.ACCEPTED.value),
        action.c.publish_status == sql_constant(PublishStatus.NOT_PUBLISHED.value),
        ~running,
        or_(
            func.coalesce(
                func.json_extract(action.c.metadata_json, sql_constant("$.publish.retryable")),
                sql_constant(1),
            )
            != sql_constant(0),
            and_(
                func.json_extract(action.c.metadata_json, sql_constant("$.publish.reconcile_required"))
                == sql_constant(1),
                executing,
            ),
        ),
        or_(retry_at.is_(None), func.julianday(retry_at) <= func.julianday(now)),
        ~attention,
        ~blocked,
    )
