"""Fenced source-round state mutations in the caller's SQLite transaction."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
import json

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from robert_agent.foundation.contracts.delivery import GitHubActionType
from robert_agent.foundation.contracts.execution import AttemptStatus
from robert_agent.foundation.contracts.work import (
    ACTIVE_TASK_LIFECYCLES,
    RequestEventState,
    SourceCapability,
    SourceIngressPhase,
    SourceRoundStatus,
    TaskLifecycle,
)
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import records, statements, transactions
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries import source_members, source_sessions as read


@dataclass(frozen=True, slots=True)
class SourceClaim:
    source_id: str
    owner: str
    token: int


def _require_transaction(conn):
    if not conn.in_transaction:
        raise ValueError("source ownership mutation requires a caller-owned transaction")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _deadline(now, seconds):
    timestamp = utc_timestamp(now)
    if timestamp is None or type(seconds) is not int or seconds <= 0:
        raise ValueError("source lease requires a timestamp and positive duration in seconds")
    return (timestamp + timedelta(seconds=seconds)).isoformat()


def _lock_session(conn, source_id):
    _require_transaction(conn)
    table = tables["source_sessions"]
    statements.write(conn, update(table).where(table.c.source_id == source_id).values(updated_at=table.c.updated_at))


def claim(conn, *, source_id, owner, now, lease_seconds):
    """Claim an idle or expired root. Existing rounds and executions stay attached."""
    if not _text(source_id) or not _text(owner):
        raise ValueError("source lease requires source and owner identities")
    lease_until = _deadline(now, lease_seconds)
    _lock_session(conn, source_id)
    source, session = (tables[name] for name in ("sources", "source_sessions"))
    current = read.session(conn, source_id)
    if current is None:
        if not statements.read(
            conn,
            select(source.c.source_id).where(
                source.c.source_id == source_id, source.c.provider == "tapd", source.c.source_type.in_(("story", "bug"))
            ),
        ):
            return None
        statements.write(
            conn,
            insert(session).values(source_id=source_id, owner=owner, token=1, lease_until=lease_until, updated_at=now),
        )
        return SourceClaim(source_id, owner, 1)
    boundary = utc_timestamp(current["lease_until"])
    last_change = utc_timestamp(current["updated_at"])
    timestamp = utc_timestamp(now)
    if boundary is None or last_change is None or timestamp < last_change or boundary > timestamp:
        return None
    token = current["token"] + 1
    statements.write(
        conn,
        update(session)
        .where(session.c.source_id == source_id, session.c.token == current["token"])
        .values(owner=owner, token=token, lease_until=lease_until, updated_at=now),
    )
    return SourceClaim(source_id, owner, token)


def lock_claim(conn, claim, *, now):
    """Acquire the root writer first and verify the current, nonexpired fence."""
    _lock_session(conn, claim.source_id)
    table = tables["source_sessions"]
    current = records.first(
        statements.read(
            conn,
            select(table).where(
                table.c.source_id == claim.source_id, table.c.owner == claim.owner, table.c.token == claim.token
            ),
        )
    )
    if current is None:
        return False
    timestamp = utc_timestamp(now)
    minimum = utc_timestamp(current["updated_at"])
    boundary = utc_timestamp(current["lease_until"])
    return timestamp is not None and minimum is not None and boundary is not None and minimum <= timestamp < boundary


def renew(conn, claim, *, now, lease_seconds):
    until = _deadline(now, lease_seconds)
    if not lock_claim(conn, claim, now=now):
        return False
    if utc_timestamp(until) < utc_timestamp(read.session(conn, claim.source_id)["lease_until"]):
        return False
    table = tables["source_sessions"]
    return bool(
        statements.write(
            conn,
            update(table)
            .where(table.c.source_id == claim.source_id, table.c.token == claim.token)
            .values(lease_until=until, updated_at=now),
        )
    )


def acquire_for_owner(conn, *, source_id, owner, now, lease_seconds):
    """Reuse the requested controller's current claim or take an expired session."""
    current = read.session(conn, source_id)
    if current and current["owner"] == owner:
        held = SourceClaim(source_id, owner, current["token"])
        if renew(conn, held, now=now, lease_seconds=lease_seconds) or lock_claim(conn, held, now=now):
            return held
    return claim(conn, source_id=source_id, owner=owner, now=now, lease_seconds=lease_seconds)


def _frozen_members(conn, source_id, member_versions):
    member, round_ = (tables[name] for name in ("source_round_requests", "source_rounds"))
    owners = source_members.pending_requests(conn, source_id, request_ids=member_versions)
    if len(owners) != len(member_versions) or any(
        ownership.request["phase"] != SourceIngressPhase.PENDING.value
        or ownership.request["content_version"] != member_versions[ownership.request["request_id"]]
        or not source_members.bound_snapshot(
            ownership, ownership.request["content_snapshot_json"], ownership.request["content_version"]
        )
        for ownership in owners
    ):
        return None
    # Each canonical event has one request membership in the frozen batch.
    ids = tuple(event["event_id"] for ownership in owners for event in ownership.events)
    if len(ids) != len(set(ids)) or statements.read(
        conn,
        select(member.c.request_id)
        .select_from(member.join(round_, round_.c.round_id == member.c.round_id))
        .where(member.c.request_id.in_(tuple(member_versions)), round_.c.status == SourceRoundStatus.ACTIVE.value)
        .limit(1),
    ):
        return None
    return tuple(ownership.request for ownership in owners)


def pending_member(conn, round_id, request_id):
    return any(row["request_id"] == request_id for row in pending_round_members(conn, round_id))


def pending_round_members(conn, round_id, *, require_current_versions=False):
    """Return the complete frozen batch only while every member remains pending."""
    cancel = tables["source_round_cancellations"]
    if statements.read(conn, select(cancel.c.round_id).where(cancel.c.round_id == round_id)):
        return ()
    members = source_members.frozen_members(conn, round_id)
    if any(
        item.ownership.request["phase"] != SourceIngressPhase.PENDING.value
        or any(event["state"] != RequestEventState.PENDING.value for event in item.ownership.events)
        or (
            require_current_versions
            and (
                item.ownership.request["content_version"] != item.member["content_version"]
                or not source_members.bound_snapshot(
                    item.ownership,
                    item.ownership.request["content_snapshot_json"],
                    item.ownership.request["content_version"],
                )
            )
        )
        for item in members
    ):
        return ()
    return tuple(item.member for item in members)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def start_round(
    conn,
    claim,
    *,
    round_id,
    member_versions,
    input_version,
    input_snapshot,
    capability,
    repo_ids,
    publication_scope,
    now,
):
    """Freeze one durable round and its pending request versions."""
    repo_ids = tuple(repo_ids)
    if (
        not _text(round_id)
        or not _text(input_version)
        or capability not in (SourceCapability.READONLY, SourceCapability.DEVELOPMENT)
        or not isinstance(input_snapshot, dict)
        or not isinstance(publication_scope, dict)
        or not isinstance(member_versions, Mapping)
        or not member_versions
        or any(not _text(key) or not _text(version) for key, version in member_versions.items())
        or any(not _text(repo_id) for repo_id in repo_ids)
    ):
        raise ValueError("invalid frozen source round input")
    encoded = _json(input_snapshot)
    repos = sorted(set(repo_ids))
    scope = _json(publication_scope)
    if (
        not lock_claim(conn, claim, now=now)
        or read.active_round(conn, claim.source_id) is not None
        or read.blockers_for_source(conn, claim.source_id, limit=1)
    ):
        return None
    members = _frozen_members(conn, claim.source_id, member_versions)
    if not members or any(row["repo_id"] is not None and row["repo_id"] not in repos for row in members):
        return None
    if any(row["provider"] == "github" for row in members):
        expected_requests = {
            row["request_id"]: {
                "request_id": row["request_id"],
                "content_version": row["content_version"],
                "input": json.loads(row["content_snapshot_json"]),
            }
            for row in members
        }
        supplied = input_snapshot.get("requests")
        if (
            not isinstance(supplied, list)
            or len(supplied) != len(expected_requests)
            or any(not isinstance(entry, dict) or not isinstance(entry.get("request_id"), str) for entry in supplied)
            or {entry["request_id"]: entry for entry in supplied} != expected_requests
        ):
            return None
    repository = tables["repos"]
    if len(
        statements.read(
            conn, select(repository.c.repo_id).where(repository.c.repo_id.in_(repos), repository.c.enabled == 1)
        )
    ) != len(repos):
        return None
    checkpoint = "start_source_round"
    transactions.savepoint(conn, checkpoint)
    try:
        statements.write(
            conn,
            insert(tables["source_rounds"]).values(
                round_id=round_id,
                source_id=claim.source_id,
                status=SourceRoundStatus.ACTIVE.value,
                capability=capability,
                repo_ids_json=_json(repos),
                publication_scope_json=scope,
                input_version=input_version,
                input_snapshot_json=encoded,
                input_sha256=sha256(encoded.encode("utf-8")).hexdigest(),
                created_at=now,
            ),
        )
        for row in members:
            statements.write(
                conn,
                insert(tables["source_round_requests"]).values(
                    round_id=round_id,
                    request_id=row["request_id"],
                    content_version=row["content_version"],
                    content_snapshot_json=row["content_snapshot_json"],
                ),
            )
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.release(conn, checkpoint)
    return read.active_round(conn, claim.source_id)


def lock_round(conn, claim, round_id, *, now):
    if not lock_claim(conn, claim, now=now):
        return None
    round_record = read.active_round(conn, claim.source_id)
    return round_record if round_record is not None and round_record["round_id"] == round_id else None


def _permits_recorded_route(conn, task, capability):
    if capability != SourceCapability.READONLY:
        return True
    if task["route_id"] in {"new-pr", "update-existing-pr"}:
        return False
    route = tables["route_decisions"]
    decisions = statements.read(
        conn,
        select(route.c.allowed_github_actions_json)
        .where(route.c.task_id == task["task_id"], route.c.route_id == task["route_id"])
        .order_by(route.c.created_at.desc(), route.c.route_decision_id.desc())
        .limit(1),
    )
    if not decisions:
        return True
    try:
        actions = json.loads(decisions[0][0])
    except (TypeError, ValueError, RecursionError):
        return False
    return isinstance(actions, list) and all(action == GitHubActionType.COMMENT for action in actions)


def bind_task(conn, claim, *, round_id, request_id, task_id, capability, now):
    """Attach one new child to the fenced round when prior executions are settled."""
    active = lock_round(conn, claim, round_id, now=now)
    if (
        active is None
        or capability not in (SourceCapability.READONLY, SourceCapability.DEVELOPMENT)
        or (capability == SourceCapability.DEVELOPMENT and active["capability"] != capability)
    ):
        return False
    member, request, task, stream, bound = (
        tables[name]
        for name in ("source_round_requests", "ingress_requests", "tasks", "workstreams", "source_round_tasks")
    )
    current = records.first(statements.read(conn, select(bound).where(bound.c.task_id == task_id)))
    if current is not None:
        return (
            current["round_id"] == round_id
            and current["request_id"] == request_id
            and current["capability"] == capability
        )
    if read.blockers_for_source(conn, claim.source_id, limit=1) or not pending_member(conn, round_id, request_id):
        return False
    scope = member.join(request, request.c.request_id == member.c.request_id)
    row = records.first(
        statements.read(
            conn,
            select(
                request.c.repo_id,
                stream.c.repo_id.label("task_repo_id"),
                task.c.lifecycle,
                task.c.task_id,
                task.c.route_id,
            )
            .select_from(
                scope.join(task, task.c.task_id == task_id).join(stream, stream.c.workstream_id == task.c.workstream_id)
            )
            .where(member.c.round_id == round_id, member.c.request_id == request_id),
        )
    )
    if (
        row is None
        or row["lifecycle"] not in ACTIVE_TASK_LIFECYCLES
        or row["task_repo_id"] not in json.loads(active["repo_ids_json"])
        or row["repo_id"] not in (None, row["task_repo_id"])
        or not _permits_recorded_route(conn, row, capability)
    ):
        return False
    return bool(
        statements.write(
            conn,
            sqlite_insert(bound)
            .values(
                task_id=task_id,
                round_id=round_id,
                request_id=request_id,
                repo_id=row["task_repo_id"],
                capability=capability,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=[bound.c.task_id]),
        )
    )


def finish_round(conn, claim, *, round_id, outcome, now):
    """Close the execution slot only. Admission/delivery state remains separate."""
    if outcome not in (
        SourceRoundStatus.EXECUTION_FINISHED,
        SourceRoundStatus.WAITING_FOR_USER,
        SourceRoundStatus.FAILED,
        SourceRoundStatus.CANCELED,
    ):
        raise ValueError("invalid source execution disposition")
    if lock_round(conn, claim, round_id, now=now) is None or read.blockers_for_source(conn, claim.source_id, limit=1):
        return False
    table = tables["source_rounds"]
    return bool(
        statements.write(
            conn,
            update(table)
            .where(table.c.round_id == round_id, table.c.status == SourceRoundStatus.ACTIVE.value)
            .values(status=outcome, finished_at=now),
        )
    )


def retire_prepared_child(conn, task_id, *, now):
    """Cancel only a proven-unlaunched queued child, preserving history and prompts."""
    attempt, task, stream = (tables[name] for name in ("attempts", "tasks", "workstreams"))
    if not statements.write(
        conn,
        update(task)
        .where(task.c.task_id == task_id, task.c.lifecycle == TaskLifecycle.QUEUED.value)
        .values(lifecycle=TaskLifecycle.CANCELED.value, updated_at=now),
    ):
        return False
    statements.write(
        conn,
        update(attempt)
        .where(attempt.c.task_id == task_id, attempt.c.status == AttemptStatus.PREPARED.value)
        .values(status=AttemptStatus.CANCELED.value, finished_at=now),
    )
    statements.write(
        conn,
        update(stream).where(stream.c.active_task_id == task_id).values(active_task_id=None, updated_at=now),
    )
    return True
