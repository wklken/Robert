"""Task-event bindings and their task-local ledger admission."""

from sqlalchemy.dialects.sqlite import insert

from robert_agent.foundation.contracts.work import RequestEventState
from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.stores.request_events import record_task_event


def bind_task_event(conn, task_id, event_id, relationship, created_at, *, ignore_duplicate=False):
    """Bind the trigger or context actually supplied to a task."""
    binding = tables["task_events"]
    stmt = insert(binding).values(task_id=task_id, event_id=event_id, relationship=relationship, created_at=created_at)
    if ignore_duplicate:
        stmt = stmt.on_conflict_do_nothing(index_elements=[binding.c.task_id, binding.c.event_id])
    return statements.write(conn, stmt)


def append_task_event(conn, task_id, event_id, relationship, now):
    if relationship == "context":
        return bind_task_event(conn, task_id, event_id, relationship, now, ignore_duplicate=True)
    if relationship != "pending":
        raise ValueError("Appended task events must be context or pending")
    # A previously bound event was ignored by legacy admission. In particular
    # rediscovery cannot requeue a result or promote an existing context.
    if bind_task_event(conn, task_id, event_id, "context", now, ignore_duplicate=True):
        return record_task_event(conn, task_id, event_id, RequestEventState.PENDING, now)
    return 0


def record_waiting_vote_reply(conn, task_id, event_id, now):
    if bind_task_event(conn, task_id, event_id, "context", now, ignore_duplicate=True):
        return record_task_event(conn, task_id, event_id, RequestEventState.CONSUMED, now, consumed_at=now)
    return 0
