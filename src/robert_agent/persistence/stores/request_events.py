"""Task and ingress consumption writes under caller-owned transactions."""

from sqlalchemy import literal, select, update
from sqlalchemy.dialects.sqlite import insert

from robert_agent.foundation.contracts.work import RequestEventState
from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables


def record_task_event(conn, task_id, event_id, state, created_at, *, consumed_at=None):
    ledger = tables["request_events"]
    stmt = insert(ledger).values(
        task_id=task_id, event_id=event_id, state=state.value, created_at=created_at, consumed_at=consumed_at
    )
    return statements.write(
        conn,
        stmt.on_conflict_do_nothing(
            index_elements=[ledger.c.task_id, ledger.c.event_id], index_where=ledger.c.task_id.is_not(None)
        ),
    )


def consume_task_result(conn, task_id, fingerprints, *, result_id, now):
    """Record accepted result evidence for bound events in this task scope."""
    ledger, binding, event, result = (
        tables[name] for name in ("request_events", "task_events", "source_events", "worker_results")
    )
    if not fingerprints:
        return 0
    # Query current membership and result ownership in the mutation. The
    # legacy path can explicitly consume a trigger or context it handled.
    matched = (
        select(
            binding.c.task_id,
            binding.c.event_id,
            literal(RequestEventState.CONSUMED.value),
            binding.c.created_at,
            literal(now),
            result.c.result_id,
        )
        .select_from(
            binding.join(event, event.c.event_id == binding.c.event_id).join(
                result, (result.c.task_id == binding.c.task_id) & (result.c.result_id == result_id)
            )
        )
        .where(binding.c.task_id == task_id, event.c.event_fingerprint.in_(fingerprints))
    )
    stmt = insert(ledger).from_select(
        ["task_id", "event_id", "state", "created_at", "consumed_at", "result_id"], matched
    )
    return statements.write(
        conn,
        stmt.on_conflict_do_update(
            index_elements=[ledger.c.task_id, ledger.c.event_id],
            index_where=ledger.c.task_id.is_not(None),
            set_={
                "state": stmt.excluded.state,
                "consumed_at": stmt.excluded.consumed_at,
                "result_id": stmt.excluded.result_id,
            },
            where=ledger.c.state == RequestEventState.PENDING.value,
        ),
    )


def enqueue_ingress_events(conn, request_id, event_ids, now):
    ledger = tables["request_events"]
    return statements.write_many(
        conn,
        insert(ledger),
        [
            {
                "request_id": request_id,
                "event_id": event_id,
                "state": RequestEventState.PENDING.value,
                "created_at": now,
            }
            for event_id in event_ids
        ],
    )


def consume_ingress_event_for_result(conn, *, request_id, event_id, result_id, now):
    """Record this exact pending member after the use case proves result authority."""
    ledger = tables["request_events"]
    return statements.write(
        conn,
        update(ledger)
        .where(
            ledger.c.request_id == request_id,
            ledger.c.event_id == event_id,
            ledger.c.state == RequestEventState.PENDING.value,
            ledger.c.result_id.is_(None),
            ledger.c.consumed_at.is_(None),
            ledger.c.source_result_id.is_(None),
        )
        .values(state=RequestEventState.CONSUMED.value, result_id=result_id, consumed_at=now),
    )
