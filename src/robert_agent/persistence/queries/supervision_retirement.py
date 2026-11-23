"""Bounded terminal-task proof, evaluated inside the supervisor's writer."""

from sqlalchemy import and_, case, func, or_, select

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.expressions import valid_json_or_empty


def candidates(conn, task_ids, now):
    if not task_ids:
        return []
    task, stream, incident = (tables[name] for name in ("tasks", "workstreams", "supervision_incidents"))
    attempt, subject, recovery = (
        tables[name] for name in ("attempts", "execution_subjects", "supervision_recovery_actions")
    )
    lineage = func.coalesce(func.json_extract(task.c.metadata_json, "$.supervision_lineage_key"), "")
    attempts = select(attempt.c.attempt_id).where(attempt.c.task_id == task.c.task_id).correlate(task)
    blocked_attempt = (
        select(1)
        .where(
            attempt.c.task_id == task.c.task_id,
            or_(
                attempt.c.status.not_in(("completed", "failed", "canceled")),
                and_(
                    or_(
                        attempt.c.started_at.is_not(None),
                        func.json_extract(attempt.c.metadata_json, "$.dispatch").is_not(None),
                    ),
                    or_(
                        func.julianday(func.json_extract(attempt.c.metadata_json, "$.process_stopped_at")).is_(None),
                        func.julianday(func.json_extract(attempt.c.metadata_json, "$.process_stopped_at"))
                        > func.julianday(now),
                    ),
                ),
            ),
        )
        .exists()
    )
    blocked_subject = (
        select(1)
        .where(
            subject.c.subject_type == "task",
            subject.c.subject_id == task.c.task_id,
            or_(
                subject.c.binding_status != "terminal",
                func.julianday(subject.c.terminal_at).is_(None),
                func.julianday(subject.c.terminal_at) > func.julianday(now),
                func.json_extract(subject.c.metadata_json, "$.dispatch_inflight").is_not(None),
            ),
        )
        .exists()
    )
    blocked_launch = (
        select(1).where(func.json_extract(subject.c.metadata_json, "$.dispatch_inflight").in_(attempts)).exists()
    )
    blocked_recovery = (
        select(1)
        .where(
            or_(
                and_(recovery.c.scope_type == "task", recovery.c.scope_id == task.c.task_id),
                and_(lineage != "", recovery.c.lineage_key == lineage),
            ),
            recovery.c.status.not_in(("succeeded", "failed", "rejected")),
        )
        .exists()
    )
    other = task.alias("lineage_task")
    blocked_lineage = (
        select(1)
        .where(
            other.c.task_id != task.c.task_id,
            lineage != "",
            func.json_extract(other.c.metadata_json, "$.supervision_lineage_key") == lineage,
            other.c.lifecycle.not_in(("canceled", "ignored")),
        )
        .exists()
    )
    lease = tables["leases"]
    blocked_lease = (
        select(1)
        .where(
            or_(
                lease.c.owner_id.in_(attempts),
                lease.c.resource_key.in_(attempts),
                func.json_extract(lease.c.metadata_json, "$.attempt_id").in_(attempts),
            ),
            lease.c.status != "released",
        )
        .exists()
    )
    stmt = (
        select(
            incident.c.incident_id,
            task.c.task_id,
            task.c.lifecycle.label("task_lifecycle"),
            task.c.updated_at.label("terminal_at"),
            stream.c.workstream_id,
            stream.c.lifecycle.label("stream_lifecycle"),
            lineage.label("lineage_key"),
        )
        .select_from(
            incident.join(task, and_(incident.c.scope_type == "task", incident.c.scope_id == task.c.task_id)).join(
                stream, stream.c.workstream_id == task.c.workstream_id
            )
        )
        .where(
            task.c.task_id.in_(tuple(task_ids)),
            task.c.task_kind == "human_request",
            task.c.lifecycle.in_(("canceled", "ignored")),
            stream.c.lifecycle.in_(("canceled", "completed", "failed")),
            or_(stream.c.active_task_id.is_(None), stream.c.active_task_id == task.c.task_id),
            incident.c.lineage_key == lineage,
            incident.c.status.in_(("open", "diagnosing", "needs_attention")),
            func.json_extract(task.c.metadata_json, "$.execution_attention").is_(None),
            ~blocked_attempt,
            ~blocked_subject,
            ~blocked_launch,
            ~blocked_recovery,
            ~blocked_lease,
            ~blocked_lineage,
        )
    )
    for name in ("review_runs", "vote_runs", "pre_pr_review_runs"):
        run = tables[name]
        session = tables[name.replace("_runs", "_sessions")]
        rounds = select(session.c.round_id).where(session.c.task_id == task.c.task_id).correlate(task)
        stmt = stmt.where(
            ~select(1)
            .where(
                or_(run.c.owner_attempt_id.in_(attempts), run.c.round_id.in_(rounds)),
                or_(
                    run.c.slot_reserved != 0,
                    run.c.status.in_(("prepared", "waiting", "starting", "running")),
                    and_(
                        run.c.dispatch_json.is_not(None),
                        func.coalesce(func.json_extract(run.c.dispatch_json, "$.process_exited"), 0) != 1,
                    ),
                ),
            )
            .exists()
        )
    return statements.read(conn, stmt)


def publications(conn, task_id):
    action, intent, result = (tables[name] for name in ("github_actions", "publication_intents", "worker_results"))
    intent_evidence = valid_json_or_empty(intent.c.remote_evidence_json)
    return statements.read(
        conn,
        select(
            action,
            intent.c.status.label("intent_status"),
            case((intent.c.publication_id.is_not(None), func.json_valid(intent.c.remote_evidence_json))).label(
                "intent_evidence_valid"
            ),
            case(
                (
                    func.json_type(intent_evidence, "$.receipt") == "object",
                    func.json_extract(intent_evidence, "$.receipt"),
                )
            ).label("intent_receipt"),
            result.c.metadata_json.label("result_metadata"),
            result.c.task_id.label("result_task_id"),
        )
        .select_from(
            action.outerjoin(intent, intent.c.action_id == action.c.action_id).outerjoin(
                result, result.c.result_id == action.c.result_id
            )
        )
        .where(action.c.task_id == task_id)
        .limit(101),
    )
