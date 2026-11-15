"""Caller-transaction writes for observed native GitHub ingress scope."""

import json

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert

from robert_agent.foundation.contracts.work import RequestEventState, SourceAuthorizationType, SourceIngressPhase
from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def hold_request(conn, *, request_id, pr_source_id, repo_id, now):
    request = tables["ingress_requests"]
    return (
        statements.write(
            conn,
            update(request)
            .where(
                request.c.request_id == request_id,
                request.c.source_id == pr_source_id,
                request.c.repo_id == repo_id,
                request.c.provider == "github",
                request.c.auth_type == SourceAuthorizationType.GITHUB_MENTION.value,
                request.c.phase == SourceIngressPhase.PENDING.value,
            )
            .values(phase=SourceIngressPhase.HELD.value, updated_at=now),
        )
        == 1
    )


def insert_observation(
    conn, *, event_id, request_id, repo_id, pr_source_id, association, target, snapshot, reason, now
):
    bridge = tables["source_github_ingress"]
    encoded = _encode(snapshot)
    return (
        statements.write(
            conn,
            insert(bridge).values(
                event_id=event_id,
                request_id=request_id,
                repo_id=repo_id,
                pr_source_id=pr_source_id,
                tapd_source_id=association.tapd_source_id,
                pr_workstream_id=association.pr_workstream_id,
                target_json=_encode(target) if target else None,
                first_observation_json=None if snapshot["replayed"] and not snapshot["predates_bridge"] else encoded,
                latest_observation_json=encoded,
                reason=reason,
                created_at=now,
                observed_at=now,
            ),
        )
        == 1
    )


def update_observation(conn, *, existing, request_id, association, target, snapshot, reason, now):
    bridge = tables["source_github_ingress"]
    encoded = _encode(snapshot)
    fields = {"latest_observation_json": encoded, "reason": reason, "observed_at": now}
    if existing["first_observation_json"] is None:
        fields["first_observation_json"] = encoded
    if request_id is not None:
        fields.update(
            request_id=request_id,
            tapd_source_id=association.tapd_source_id,
            pr_workstream_id=association.pr_workstream_id,
            target_json=_encode(target),
        )
    return (
        statements.write(
            conn,
            update(bridge)
            .where(
                bridge.c.event_id == existing["event_id"],
                bridge.c.repo_id == existing["repo_id"],
                bridge.c.pr_source_id == existing["pr_source_id"],
                bridge.c.latest_observation_json == existing["latest_observation_json"],
                bridge.c.observed_at == existing["observed_at"],
                bridge.c.request_id.is_(existing["request_id"])
                if existing["request_id"] is None
                else bridge.c.request_id == existing["request_id"],
            )
            .values(**fields),
        )
        == 1
    )


def save_current_observation(conn, *, request_id, operation_id, fields, initial):
    """Reserve a probe identity or replace only the same operation's placeholder."""
    current = tables["source_github_observations"]
    if initial:
        stmt = insert(current).values(request_id=request_id, operation_id=operation_id, **fields)
        stmt = stmt.on_conflict_do_update(
            index_elements=[current.c.request_id],
            set_={"operation_id": operation_id, **fields},
        )
    else:
        stmt = (
            update(current)
            .where(current.c.request_id == request_id, current.c.operation_id == operation_id)
            .values(**fields)
        )
    return statements.write(conn, stmt) == 1


def replace_current_grant(conn, *, owned, phase, now, authorization=None, snapshot=None, version=None):
    """Amend the exact current GitHub-owned request and only its pending event."""
    if not conn.in_transaction or owned.return_target is None or len(owned.events) != 1:
        return False
    old, event = owned.request, owned.events[0]
    request, ledger = (tables[name] for name in ("ingress_requests", "request_events"))
    pending = select(ledger.c.row_id).where(
        ledger.c.row_id == event["row_id"],
        ledger.c.request_id == old["request_id"],
        ledger.c.event_id == event["event_id"],
        ledger.c.state == RequestEventState.PENDING.value,
    )
    fields = {"phase": phase.value}
    if authorization is not None:
        fields.update(
            authorization_json=_encode(authorization), content_snapshot_json=_encode(snapshot), content_version=version
        )
    if all(old[key] == value for key, value in fields.items()):
        return phase != SourceIngressPhase.REVOKED
    changed = statements.write(
        conn,
        update(request)
        .where(
            request.c.request_id == old["request_id"],
            request.c.provider == "github",
            request.c.auth_type == SourceAuthorizationType.GITHUB_MENTION.value,
            request.c.phase == old["phase"],
            request.c.content_version == old["content_version"],
            request.c.authorization_json == old["authorization_json"],
            request.c.content_snapshot_json == old["content_snapshot_json"],
            request.c.updated_at == old["updated_at"],
            pending.exists(),
        )
        .values(**fields, updated_at=now),
    )
    if changed != 1:
        return False
    if phase == SourceIngressPhase.REVOKED:
        return (
            statements.write(
                conn, update(ledger).where(ledger.c.row_id.in_(pending)).values(state=RequestEventState.REVOKED.value)
            )
            == 1
        )
    return True
