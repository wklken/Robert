"""Exact ownership of native source requests and their canonical event members."""

import json
import re
from dataclasses import dataclass

from sqlalchemy import or_, select

from robert_agent.foundation.contracts.delivery import SourceResultKind
from robert_agent.foundation.contracts.work import RequestEventState, SourceAuthorizationType, SourceIngressPhase
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries import ingress_requests, source_github_ingress, source_pr_lineage
from robert_agent.persistence.rounds.source_evidence import context_digest, json_object

_DISCUSSION = {"comment": "issuecomment-", "review": "pullrequestreview-", "review_comment": "discussion_r"}


@dataclass(frozen=True, slots=True)
class RequestOwnership:
    request: records.Record
    events: tuple[records.Record, ...]
    return_target: dict | None = None
    bridge: records.Record | None = None
    creation: source_pr_lineage.ConfirmedPRLineage | None = None
    original_item: dict | None = None
    original_item_proven: bool = False


@dataclass(frozen=True, slots=True)
class FrozenMember:
    member: records.Record
    ownership: RequestOwnership


def _event_members(conn, request_id):
    ledger, event = (tables[name] for name in ("request_events", "source_events"))
    return tuple(
        statements.read(
            conn,
            select(
                ledger,
                event.c.source_id.label("event_source_id"),
                event.c.repo_id.label("event_repo_id"),
                event.c.event_type,
                event.c.event_fingerprint,
            )
            .select_from(ledger.outerjoin(event, event.c.event_id == ledger.c.event_id))
            .where(ledger.c.request_id == request_id)
            .order_by(ledger.c.row_id),
        )
    )


def _incompatible_other_ownership(conn, source_id, request_id, event_id):
    ledger, tasks, bound, member, round_ = (
        tables[name]
        for name in ("request_events", "task_events", "source_round_tasks", "source_round_requests", "source_rounds")
    )
    other_request = select(ledger.c.row_id).where(
        ledger.c.event_id == event_id, ledger.c.request_id.is_not(None), ledger.c.request_id != request_id
    )
    native_task = (
        select(bound.c.task_id)
        .select_from(
            bound.join(round_, round_.c.round_id == bound.c.round_id).join(
                member, member.c.round_id == round_.c.round_id
            )
        )
        .where(bound.c.task_id == tasks.c.task_id, round_.c.source_id == source_id, member.c.request_id == request_id)
        .correlate(tasks)
        .exists()
    )
    other_task = select(tasks.c.task_id).where(tasks.c.event_id == event_id, ~native_task)
    return bool(statements.read(conn, select(or_(other_request.exists(), other_task.exists())))[0][0])


def _github_owner(conn, source_id, request, events):
    bridge = source_github_ingress.for_request(conn, request["request_id"])
    if (
        bridge is None
        or bridge["tapd_source_id"] != source_id
        or request["auth_type"] != SourceAuthorizationType.GITHUB_MENTION.value
        or request["repo_id"] != bridge["repo_id"]
        or request["source_id"] != bridge["pr_source_id"]
        or len(events) != 1
    ):
        return None
    event = events[0]
    event_id, event_type = event["event_id"], event["event_type"]
    if (
        event_id != bridge["event_id"]
        or event["event_repo_id"] != bridge["repo_id"]
        or event_type not in _DISCUSSION
        or _incompatible_other_ownership(conn, source_id, request["request_id"], event_id)
    ):
        return None
    source = source_github_ingress.source(conn, bridge["pr_source_id"])
    association = source_github_ingress.association(conn, source, bridge["repo_id"])
    lineage = association.lineage if association else None
    snapshot = json_object(request["content_snapshot_json"]) or {}
    item = snapshot.get("item")
    target = json_object(bridge["target_json"])
    if (
        lineage is None
        or association.tapd_source_id != source_id
        or bridge["pr_workstream_id"] != lineage.workstream_id
        or target is None
        or not isinstance(item, dict)
    ):
        return None
    identifier, url = str(item.get("id")), item.get("url")
    if not re.fullmatch(r"[1-9][0-9]*", identifier) or not isinstance(url, str):
        return None
    path, delimiter, anchor = url.partition("#")
    if (
        not delimiter
        or anchor != _DISCUSSION[event_type] + identifier
        or not source_pr_lineage.same_pr_url(path, lineage.url)
        or event["event_fingerprint"] != f"{event_type}:{identifier}"
        or event_id != "event:" + event["event_fingerprint"]
        or not isinstance(item.get("author"), str)
        or utc_timestamp(item.get("created_at")) is None
        or utc_timestamp(item.get("updated_at")) is None
    ):
        return None
    expected = {
        "provider": "github",
        "host": "github.com",
        "repo_id": lineage.repo_id,
        "repository": lineage.repository,
        "pr_number": lineage.number,
        "pr_url": lineage.url,
        "pr_source_id": lineage.pr_source_id,
        "pr_workstream_id": lineage.workstream_id,
        "event_id": event_id,
        "event_type": event_type,
        "event_fingerprint": event["event_fingerprint"],
        "item_id": identifier,
        "event_url": url,
        "tapd_source_id": source_id,
        "origin_workstream_id": lineage.origin_workstream_id,
        "creation_result_id": lineage.source_result_id,
        "creation_action_id": lineage.action_id,
        "creation_publication_id": lineage.publication_id,
        "creation_request_fingerprint": lineage.request_fingerprint,
    }
    if target != expected:
        return None
    first = json_object(bridge["first_observation_json"]) or {}
    original = first.get("item")
    if isinstance(original, dict) and original:
        if (
            str(original.get("id")) != identifier
            or original.get("url") != url
            or original.get("author") != item["author"]
            or utc_timestamp(original.get("created_at")) != utc_timestamp(item["created_at"])
        ):
            return None
        # The immutable identity is observed in the first item. The editable
        # view supplies only the currently recorded update-time floor.
        item = {**original, "updated_at": item["updated_at"]}
        return RequestOwnership(request, events, target, bridge, lineage, item, True)
    # Old-shaped first observations could lack item facts while the original
    # held admission captured them. Keep the held read path. Freezing rejects
    # these members until a producer can establish an immutable identity.
    return RequestOwnership(request, events, target, bridge, lineage, item)


def owned_request(conn, source_id, request_id):
    """Return exact original provider/event ownership for this TAPD session."""
    request = ingress_requests.find_by_id(conn, request_id)
    if request is None:
        return None
    events = _event_members(conn, request_id)
    if not events or any(event["event_source_id"] != request["source_id"] for event in events):
        return None
    if request["provider"] == "github":
        return _github_owner(conn, source_id, request, events)
    source = source_github_ingress.source(conn, source_id)
    if (
        request["provider"] != "tapd"
        or request["source_id"] != source_id
        or source is None
        or source["provider"] != "tapd"
    ):
        return None
    return RequestOwnership(request, events)


def pending_requests(conn, source_id, *, request_ids=None):
    """Read members whose ledger is pending, including requests still HELD."""
    request, bridge, ledger = (tables[name] for name in ("ingress_requests", "source_github_ingress", "request_events"))
    member = select(ledger.c.row_id).where(ledger.c.request_id == request.c.request_id)
    linked = select(bridge.c.request_id).where(
        bridge.c.request_id == request.c.request_id, bridge.c.tapd_source_id == source_id
    )
    stmt = (
        select(request.c.request_id)
        .where(
            ((request.c.source_id == source_id) & (request.c.provider == "tapd"))
            | ((request.c.provider == "github") & linked.exists()),
            member.where(ledger.c.state == RequestEventState.PENDING.value).exists(),
            ~member.where(ledger.c.state != RequestEventState.PENDING.value).exists(),
        )
        .order_by(request.c.created_at, request.c.request_id)
    )
    if request_ids is not None:
        stmt = stmt.where(request.c.request_id.in_(tuple(request_ids)))
    result = []
    for row in statements.read(conn, stmt):
        ownership = owned_request(conn, source_id, row[0])
        if ownership is not None:
            result.append(ownership)
    return tuple(result)


def github_content_version(event_id, body, actor):
    """Version GitHub work by canonical identity and semantic body/actor."""
    return context_digest([event_id, body, actor.casefold()])


def bound_snapshot(ownership, encoded, content_version):
    """Recheck the frozen GitHub trigger identity independently of newer body text."""
    snapshot = json_object(encoded)
    if snapshot is None:
        return False
    target = ownership.return_target
    if target is None:
        return True
    item = snapshot.get("item")
    original = ownership.original_item
    return (
        ownership.original_item_proven
        and snapshot.get("event_id") == target["event_id"]
        and snapshot.get("event_fingerprint") == target["event_fingerprint"]
        and isinstance(snapshot.get("body"), str)
        and isinstance(snapshot.get("actor_login"), str)
        and github_content_version(target["event_id"], snapshot["body"], snapshot["actor_login"]) == content_version
        and isinstance(item, dict)
        and str(item.get("id")) == target["item_id"]
        and item.get("url") == target["event_url"]
        and item.get("author") == original["author"]
        and utc_timestamp(item.get("created_at")) == utc_timestamp(original["created_at"])
    )


def round_github_request_ids(conn, round_id):
    """Find the exact stored GitHub members even when a member's evidence is malformed."""
    member, request = (tables[name] for name in ("source_round_requests", "ingress_requests"))
    return tuple(
        row[0]
        for row in statements.read(
            conn,
            select(member.c.request_id)
            .select_from(member.join(request, request.c.request_id == member.c.request_id))
            .where(member.c.round_id == round_id, request.c.provider == "github")
            .order_by(member.c.request_id),
        )
    )


def frozen_members(conn, round_id):
    """Load the complete frozen batch, or empty if any binding is unproved."""
    round_, member = (tables[name] for name in ("source_rounds", "source_round_requests"))
    source = records.first(
        statements.read(conn, select(round_.c.source_id, round_.c.repo_ids_json).where(round_.c.round_id == round_id))
    )
    if source is None:
        return ()
    rows = statements.read(conn, select(member).where(member.c.round_id == round_id).order_by(member.c.request_id))
    result = []
    for row in rows:
        owned = owned_request(conn, source[0], row["request_id"])
        if owned is None or not bound_snapshot(owned, row["content_snapshot_json"], row["content_version"]):
            return ()
        if owned.return_target is not None:
            try:
                repositories = json.loads(source["repo_ids_json"])
            except (TypeError, ValueError, RecursionError):
                return ()
            if not isinstance(repositories, list) or owned.request["repo_id"] not in repositories:
                return ()
        result.append(FrozenMember(row, owned))
    return tuple(result)


def handled_return_targets(conn, round_id, handled_members):
    """Rebuild GitHub return targets from exact frozen members and their original bridges."""
    members = frozen_members(conn, round_id)
    if not members or not isinstance(handled_members, (list, tuple)) or not handled_members:
        return None
    request_ids = [member["request_id"] for member in handled_members]
    if len(request_ids) != len(set(request_ids)) or not set(request_ids) <= {
        member.member["request_id"] for member in members
    }:
        return None
    return [
        {"request_id": member.member["request_id"], "target": member.ownership.return_target}
        for member in members
        if member.member["request_id"] in request_ids and member.ownership.return_target is not None
    ]


def accepted_round_members(conn, root, round_):
    """Read original frozen ownership, distinguishing handled and still-pending members."""
    if (
        root["source_id"] != round_["source_id"]
        or root["round_id"] != round_["round_id"]
        or root["input_version"] != round_["input_version"]
    ):
        return ()
    evidence = json_object(root["evidence_json"]) or {}
    snapshot = json_object(round_["input_snapshot_json"]) or {}
    authority, handled = snapshot.get("authority"), evidence.get("handled_members")
    if not isinstance(authority, dict) or not isinstance(handled, list) or not handled:
        return ()
    consumed = consumed_root_requests(conn, root["source_id"], root["round_id"])
    if (
        not consumed
        or [
            {
                "request_id": member.member["request_id"],
                "content_version": member.member["content_version"],
                "events": [
                    {"row_id": row["row_id"], "event_id": row["event_id"], "fingerprint": row["event_fingerprint"]}
                    for row in member.ownership.events
                ],
            }
            for member in consumed
        ]
        != handled
    ):
        return ()
    consumed_ids = {item.member["request_id"] for item in consumed}
    members = frozen_members(conn, round_["round_id"])
    if not members:
        return ()
    for member in members:
        request_id = member.member["request_id"]
        owned = member.ownership
        if (
            owned.request["phase"] != SourceIngressPhase.PENDING
            or json_object(owned.request["authorization_json"]) != authority.get(request_id)
            or (
                request_id not in consumed_ids
                and any(
                    row["state"] != RequestEventState.PENDING
                    or any(row[key] is not None for key in ("source_result_id", "consumed_at", "result_id"))
                    for row in owned.events
                )
            )
        ):
            return ()
    return members


def disposition_members(conn, root, round_):
    """Prove original accepted membership without treating a live phase as historical authority."""
    if (root["source_id"], root["round_id"], root["input_version"]) != (
        round_["source_id"],
        round_["round_id"],
        round_["input_version"],
    ):
        return ()
    snapshot = json_object(round_["input_snapshot_json"]) or {}
    evidence = json_object(root["evidence_json"]) or {}
    frozen = snapshot.get("authority")
    members = frozen_members(conn, round_["round_id"])
    handled = consumed_root_requests(conn, root["source_id"], root["round_id"])
    if not isinstance(frozen, dict) or not members or not handled:
        return ()
    material = [
        {
            "request_id": member.member["request_id"],
            "content_version": member.member["content_version"],
            "events": [
                {"row_id": event["row_id"], "event_id": event["event_id"], "fingerprint": event["event_fingerprint"]}
                for event in member.ownership.events
            ],
        }
        for member in handled
    ]
    if material != evidence.get("handled_members") or set(frozen) != {item.member["request_id"] for item in members}:
        return ()
    for member in members:
        owned = member.ownership
        original = frozen[member.member["request_id"]]
        if (
            not isinstance(original, dict)
            or not owned.events
            or original.get("event_id") not in {event["event_id"] for event in owned.events}
        ):
            return ()
    return members


def matches_disposition_fact(member, fact, *, accepted_at):
    """Recognize the original GitHub identity recorded by a sealed disposition observation."""
    target = member.ownership.return_target
    observed, accepted = utc_timestamp(fact.get("observed_at")), utc_timestamp(accepted_at)
    return (
        target is not None
        and set(fact)
        == {"reason", "request_id", "event_id", "target_sha256", "operation_id", "observation_hash", "observed_at"}
        and fact["request_id"] == member.member["request_id"]
        and fact["event_id"] == target["event_id"]
        and fact["target_sha256"] == context_digest(target)
        and isinstance(fact["operation_id"], str)
        and bool(fact["operation_id"])
        and isinstance(fact["observation_hash"], str)
        and bool(re.fullmatch(r"[0-9a-f]{64}", fact["observation_hash"]))
        and observed is not None
        and accepted is not None
        and accepted <= observed
    )


def disposition_consumed_evidence(conn, root, round_):
    """Materialize immutable handled ledger rows and frozen ownership for local disposition."""
    members = disposition_members(conn, root, round_)
    snapshot = json_object(round_["input_snapshot_json"]) or {}
    frozen = snapshot.get("authority", {})
    return tuple(
        {
            "request_id": member.member["request_id"],
            "auth_type": member.ownership.request["auth_type"],
            "authorization": frozen[member.member["request_id"]],
            "events": [dict(event) for event in member.ownership.events],
            "github_target": member.ownership.return_target,
        }
        for member in members
        if member.ownership.events[0]["source_result_id"] == root["source_result_id"]
    )


def consumed_root_requests(conn, source_id, round_id):
    """Return exactly consumed frozen ownership for this same durable root."""
    root, round_ = (tables[name] for name in ("source_results", "source_rounds"))
    row = records.first(
        statements.read(
            conn,
            select(root.c.source_result_id)
            .select_from(
                root.join(round_, (round_.c.round_id == root.c.round_id) & (round_.c.source_id == root.c.source_id))
            )
            .where(
                root.c.source_id == source_id,
                root.c.round_id == round_id,
                root.c.input_version == round_.c.input_version,
                root.c.kind.in_((SourceResultKind.ANSWER.value, SourceResultKind.QUESTION.value)),
            ),
        )
    )
    if row is None:
        return ()
    result = []
    for member in frozen_members(conn, round_id):
        events = member.ownership.events
        if not any(event["source_result_id"] == row[0] for event in events):
            continue
        if any(
            event["source_result_id"] != row[0]
            or event["state"] != RequestEventState.CONSUMED.value
            or not event["consumed_at"]
            or event["result_id"] is not None
            for event in events
        ):
            return ()
        result.append(member)
    ledger = tables["request_events"]
    exact = statements.read(conn, select(ledger.c.row_id).where(ledger.c.source_result_id == row[0]))
    expected = {event["row_id"] for member in result for event in member.ownership.events}
    return tuple(result) if expected == {entry[0] for entry in exact} else ()
