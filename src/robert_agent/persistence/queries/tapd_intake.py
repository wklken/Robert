"""Materialized records for native TAPD observations and cancellation."""

from sqlalchemy import select

from robert_agent.foundation.contracts.work import RequestEventState

from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables


def event(conn, event_id):
    table = tables["source_events"]
    return records.first(statements.read(conn, select(table).where(table.c.event_id == event_id)))


def events(conn, source_id, event_type):
    table = tables["source_events"]
    return statements.read(
        conn,
        select(table)
        .where(table.c.source_id == source_id, table.c.event_type == event_type)
        .order_by(table.c.event_at, table.c.event_id),
    )


def pending_requests(conn, source_id, *, request_id=None):
    table = tables["ingress_requests"]
    ledger = tables["request_events"]
    pending = (
        select(ledger.c.row_id)
        .where(ledger.c.request_id == table.c.request_id, ledger.c.state == RequestEventState.PENDING.value)
        .exists()
    )
    nonpending = (
        select(ledger.c.row_id)
        .where(ledger.c.request_id == table.c.request_id, ledger.c.state != RequestEventState.PENDING.value)
        .exists()
    )
    query = (
        select(table)
        .where(table.c.source_id == source_id, table.c.provider == "tapd", pending, ~nonpending)
        .order_by(table.c.created_at, table.c.request_id)
    )
    if request_id is not None:
        query = query.where(table.c.request_id == request_id)
    return statements.read(conn, query)


def consumed_root_requests(conn, source_id, round_id):
    """Read consumed requests owned by this exact root for revocation observation."""
    request, root, ledger = (tables[name] for name in ("ingress_requests", "source_results", "request_events"))
    consumed = (
        select(ledger.c.row_id)
        .select_from(ledger.join(root, root.c.source_result_id == ledger.c.source_result_id))
        .where(
            ledger.c.request_id == request.c.request_id,
            ledger.c.state == RequestEventState.CONSUMED.value,
            root.c.source_id == source_id,
            root.c.round_id == round_id,
        )
        .exists()
    )
    return statements.read(
        conn,
        select(request)
        .where(request.c.source_id == source_id, request.c.provider == "tapd", consumed)
        .order_by(request.c.request_id),
    )


def round_cancellation(conn, round_id):
    table = tables["source_round_cancellations"]
    return records.first(statements.read(conn, select(table).where(table.c.round_id == round_id)))


def activation(conn, host, workspace_id):
    table = tables["tapd_project_intake"]
    return records.first(
        statements.read(conn, select(table).where(table.c.host == host, table.c.workspace_id == workspace_id))
    )


def source_observation(conn, source_id):
    source = tables["sources"]
    return records.first(
        statements.read(
            conn, select(source.c.metadata_json).where(source.c.source_id == source_id, source.c.provider == "tapd")
        )
    )


def repository_ids(conn, names):
    table = tables["repos"]
    return {
        row["full_name"]: row["repo_id"]
        for row in statements.read(
            conn, select(table.c.full_name, table.c.repo_id).where(table.c.full_name.in_(names), table.c.enabled == 1)
        )
    }
