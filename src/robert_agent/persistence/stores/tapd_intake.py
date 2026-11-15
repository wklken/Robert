"""Caller-transaction mutations for TAPD source evidence, ingress and cancellation."""

import json

from sqlalchemy import literal, select, update
from sqlalchemy.dialects.sqlite import insert

from robert_agent.foundation.contracts.work import RequestEventState, SourceIngressPhase, SourceRoundStatus
from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries import tapd_intake as read


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def lock_observation(conn, source_id):
    if not conn.in_transaction:
        raise ValueError("TAPD observation requires a caller-owned writer transaction")
    source = tables["sources"]
    statements.write(conn, update(source).where(source.c.source_id == source_id).values(title=source.c.title))


def remember_activation(conn, host, workspace_id, cutoff, now):
    table = tables["tapd_project_intake"]
    statements.write(
        conn,
        insert(table)
        .values(host=host, workspace_id=workspace_id, activated_after=cutoff, created_at=now)
        .on_conflict_do_nothing(index_elements=[table.c.host, table.c.workspace_id]),
    )
    return read.activation(conn, host, workspace_id)["activated_after"]


def save_event(conn, *, source_id, event_id, event_type, happened_at, payload):
    table = tables["source_events"]
    stmt = insert(table).values(
        event_id=event_id,
        source_id=source_id,
        event_fingerprint=event_id,
        event_type=event_type,
        event_at=happened_at,
        payload_json=_json(payload),
        actor_kind="tapd_observation",
        authorization_status="pending",
    )
    changed = statements.write(
        conn,
        stmt.on_conflict_do_update(
            index_elements=[table.c.event_id],
            set_={"payload_json": stmt.excluded.payload_json},
            where=(table.c.source_id == source_id)
            & (table.c.event_type == event_type)
            & (table.c.event_fingerprint == event_id),
        ),
    )
    if changed != 1:
        raise ValueError("TAPD event identity cannot be rebound")


def amend_request(conn, *, source_id, request_id, now, phase=None, snapshot=None, version=None, repo_id=None):
    table = tables["ingress_requests"]
    values = {"updated_at": now}
    if phase is not None:
        values["phase"] = phase
    if snapshot is not None:
        values.update(content_snapshot_json=_json(snapshot), content_version=version)
    if repo_id is not None:
        values["repo_id"] = repo_id
    changed = statements.write(
        conn,
        update(table)
        .where(
            table.c.request_id == request_id,
            table.c.source_id == source_id,
            table.c.provider == "tapd",
            table.c.phase != SourceIngressPhase.REVOKED.value,
        )
        .values(**values),
    )
    if changed and phase == SourceIngressPhase.REVOKED:
        ledger = tables["request_events"]
        statements.write(
            conn,
            update(ledger)
            .where(
                ledger.c.request_id == request_id,
                ledger.c.state == RequestEventState.PENDING.value,
            )
            .values(state=RequestEventState.REVOKED.value),
        )
    return bool(changed)


def request_cancellation(conn, *, source_id, round_id, reason, now):
    table, rounds = (tables[name] for name in ("source_round_cancellations", "source_rounds"))
    return bool(
        statements.write(
            conn,
            insert(table)
            .from_select(
                ("round_id", "reason", "requested_at"),
                select(rounds.c.round_id, literal(reason), literal(now)).where(
                    rounds.c.round_id == round_id,
                    rounds.c.source_id == source_id,
                    rounds.c.status == SourceRoundStatus.ACTIVE.value,
                ),
            )
            .on_conflict_do_nothing(index_elements=[table.c.round_id]),
        )
    )
