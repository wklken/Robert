"""Persist bounded source progress and settle proven canceled native work."""

import json

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert

from robert_agent.foundation.contracts.delivery import PublishStatus
from robert_agent.foundation.contracts.execution import AttemptStatus
from robert_agent.foundation.contracts.work import TaskLifecycle
from robert_agent.persistence import execution_control, statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries import source_results, source_sessions as read, tapd_intake
from robert_agent.persistence.stores import source_sessions


def save(conn, claim, *, now, next_due_at, status, reason=None, round_id=None):
    if not source_sessions.lock_claim(conn, claim, now=now):
        return False
    table = tables["tapd_coordination"]
    fields = dict(updated_at=now, next_due_at=next_due_at, status=status, reason=reason, round_id=round_id)
    return bool(
        statements.write(
            conn,
            insert(table)
            .values(source_id=claim.source_id, **fields)
            .on_conflict_do_update(index_elements=[table.c.source_id], set_=fields),
        )
    )


def settle_canceled_worker(conn, claim, round_id, task_id, *, now, attempt_updates=()):
    """Commit already-observed exits and skip only candidates without an effect intent."""
    if (
        source_sessions.lock_round(conn, claim, round_id, now=now) is None
        or tapd_intake.round_cancellation(conn, round_id) is None
        or task_id not in {row["task_id"] for row in read.round_tasks(conn, round_id)}
    ):
        return False
    binding = execution_control.get_binding(conn, "task", task_id)
    if binding is None:
        return False
    try:
        checkpoint = json.loads(binding["metadata_json"])
    except (TypeError, ValueError):
        return False
    if not isinstance(checkpoint, dict) or checkpoint.get("dispatch_inflight") is not None:
        return False
    updates = {old["attempt_id"]: (old, outcome) for old, outcome in attempt_updates}
    attempt, task, stream, action, intent = (
        tables[name] for name in ("attempts", "tasks", "workstreams", "github_actions", "publication_intents")
    )
    current = [row for row in read.worker_attempts(conn, round_id) if row["task_id"] == task_id]
    if not current or any(row != updates[row["attempt_id"]][0] for row in current if row["attempt_id"] in updates):
        return False
    if not updates.keys() <= {row["attempt_id"] for row in current} or any(
        row["attempt_id"] not in updates
        and not read.attempt_is_settled(conn, task_id, row["attempt_id"], subject_type="task", subject_id=task_id)
        for row in current
    ):
        return False
    root = source_results.for_round(conn, round_id)
    accepted = (
        source_results.worker_candidate(conn, root["worker_result_id"])
        if root is not None and root["source_id"] == claim.source_id and root["worker_result_id"] is not None
        else None
    )
    for row, outcome in updates.values():
        if outcome.get("status") not in {"terminated", "already_dead", "not_found"}:
            return False
        meta = json.loads(row["metadata_json"])
        meta.update(termination=outcome, process_stopped_at=now)
        fields = {"metadata_json": json.dumps(meta, sort_keys=True)}
        if (
            accepted is None
            or accepted["task_id"] != task_id
            or accepted["attempt_id"] != row["attempt_id"]
            or row["status"] != AttemptStatus.COMPLETED
        ):
            fields.update(status=AttemptStatus.CANCELED, finished_at=now)
        statements.write(
            conn,
            update(attempt).where(attempt.c.attempt_id == row["attempt_id"]).values(**fields),
        )
    statements.write(
        conn,
        update(attempt)
        .where(attempt.c.task_id == task_id, attempt.c.status == AttemptStatus.PREPARED)
        .values(status=AttemptStatus.CANCELED, finished_at=now),
    )
    statements.write(
        conn,
        update(task)
        .where(
            task.c.task_id == task_id,
            task.c.lifecycle.in_(
                (
                    TaskLifecycle.QUEUED,
                    TaskLifecycle.RUNNING,
                    TaskLifecycle.WAITING_FOR_USER,
                )
            ),
        )
        .values(lifecycle=TaskLifecycle.CANCELED, updated_at=now),
    )
    statements.write(
        conn,
        update(stream).where(stream.c.active_task_id == task_id).values(active_task_id=None, updated_at=now),
    )
    execution_control.close_binding(conn, "task", task_id, now)
    required = tables["source_required_actions"]
    statements.write(
        conn,
        update(action)
        .where(
            action.c.task_id == task_id,
            action.c.publish_status == PublishStatus.NOT_PUBLISHED,
            ~select(intent.c.action_id).where(intent.c.action_id == action.c.action_id).exists(),
            ~select(required.c.action_id).where(required.c.action_id == action.c.action_id).exists(),
        )
        .values(publish_status=PublishStatus.SKIPPED),
    )
    return True
