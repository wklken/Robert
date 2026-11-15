"""Read exact local disposition of accepted source results and their effects."""

import re
from hashlib import sha256

from sqlalchemy import select

from robert_agent.foundation.contracts.delivery import PublicationStatus, PublishStatus
from robert_agent.foundation.contracts.work import SourceRoundStatus
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries import (
    source_delivery_pauses,
    source_delivery_recovery,
    source_members,
    source_results,
    tapd_intake,
)
from robert_agent.persistence.rounds.source_evidence import context_digest, json_object

GITHUB_WITHDRAWAL_REASONS = frozenset(
    {
        "github_item_absence_confirmed",
        "github_current_actor_untrusted",
        "github_current_creator_changed",
        "github_current_mention_removed",
    }
)


def for_root(conn, source_result_id):
    table = tables["source_result_retirements"]
    return records.first(statements.read(conn, select(table).where(table.c.source_result_id == source_result_id)))


def historical_roots(conn, source_id):
    roots, rounds, cancel, retired = (
        tables[name]
        for name in ("source_results", "source_rounds", "source_round_cancellations", "source_result_retirements")
    )
    return statements.read(
        conn,
        select(roots.c.source_result_id, roots.c.round_id)
        .select_from(
            roots.join(rounds, rounds.c.round_id == roots.c.round_id).join(
                cancel, cancel.c.round_id == rounds.c.round_id
            )
        )
        .where(
            roots.c.source_id == source_id,
            rounds.c.source_id == source_id,
            rounds.c.status == "canceled",
            ~select(1).where(retired.c.source_result_id == roots.c.source_result_id).exists(),
        )
        .order_by(roots.c.accepted_at, roots.c.source_result_id),
    )


def commitment(conn, root, round_, cancellation):
    source = source_results.tapd_source(conn, root["source_id"])
    members = (
        source_members.disposition_consumed_evidence(conn, root, round_)
        if source_members.round_github_request_ids(conn, round_["round_id"])
        else source_results.consumed_members(conn, root, round_)
    )
    evidence = json_object(root["evidence_json"]) or {}
    if (
        source is None
        or not members
        or cancellation is None
        or root["kind"] not in {"answer", "question"}
        or (root["source_id"], root["round_id"], root["input_version"])
        != (round_["source_id"], round_["round_id"], round_["input_version"])
        or round_["input_sha256"] != evidence.get("round_input_sha256")
        or round_["input_sha256"] != sha256(round_["input_snapshot_json"].encode()).hexdigest()
        or cancellation["round_id"] != root["round_id"]
        or utc_timestamp(root["accepted_at"]) is None
        or utc_timestamp(cancellation["requested_at"]) is None
        or utc_timestamp(cancellation["requested_at"]) < utc_timestamp(root["accepted_at"])
    ):
        return None
    target = source_results.source_target(source)
    if json_object(round_["publication_scope_json"]) != {**target, "action": "comment"}:
        return None
    return {
        "target": target,
        "root_sha256": context_digest(dict(root)),
        "round_sha256": context_digest(
            {key: value for key, value in dict(round_).items() if key not in {"status", "finished_at"}}
        ),
        "cancellation_sha256": context_digest(dict(cancellation)),
        "member_sha256": context_digest([dict(member) for member in members]),
    }


def action_plan(conn, root, actions, target):
    sets = source_results.required_action_sets(conn, root, actions)
    if (
        sets is None
        or not sets[0]
        or sets[0][-1]["provider"] != "tapd"
        or not source_delivery_pauses.exact_action_set(conn, root, actions)
    ):
        return None
    first_missing = next(
        (i for i, row in enumerate(actions) if row["publish_status"] != PublishStatus.PUBLISHED), len(actions)
    )
    if (
        source_results.confirmed_required_effects(
            conn,
            root["source_result_id"],
            target,
            before_position=first_missing if first_missing < len(actions) else None,
        )
        is None
    ):
        return None
    if any(
        row["position"] != position or not source_delivery_pauses.locally_never_started(conn, root, row, target)
        for position, row in enumerate(actions[first_missing:], first_missing)
    ):
        return None
    return first_missing


def action_digests(actions):
    return [source_delivery_pauses.effect_digest(action) for action in actions]


def intent_disposition(source_result_id, action_id):
    return {"disposition": "local_never_started", "source_result_id": source_result_id, "action_id": action_id}


def _original_actions(rows, root_id, first_missing, retired_at):
    """Rebuild pre-retirement effects only from exact final local states."""
    original = [dict(row) for row in rows]
    for row in original[first_missing:]:
        if row["publish_status"] != PublishStatus.SKIPPED:
            return None
        row["publish_status"] = PublishStatus.NOT_PUBLISHED.value
        if row["publication_id"] is not None:
            if (
                row["intent_status"] != PublicationStatus.FAILED
                or row["intent_finished_at"] != retired_at
                or row["intent_updated_at"] != retired_at
                or json_object(row["intent_error_json"]) != intent_disposition(root_id, row["action_id"])
            ):
                return None
            row.update(
                intent_status=PublicationStatus.PREPARED.value,
                intent_finished_at=None,
                intent_updated_at=row["intent_created_at"],
                intent_error_json=None,
            )
    return original


def is_retired(conn, root_id):
    row = for_root(conn, root_id)
    if row is None:
        return False
    try:
        return _verified(conn, root_id, row)
    except (IndexError, KeyError, TypeError, ValueError, RecursionError):
        return False


def _valid_cause(conn, root, round_, cause):
    if not re.fullmatch(r"[0-9a-f]{64}", cause["observation_hash"]):
        return False
    if cause["reason"] == "terminal":
        statuses = cause["terminal_states"]
        return cause["archived"] is True or (
            isinstance(statuses, list) and bool(cause["status"]) and cause["status"] in statuses
        )
    mixed = bool(source_members.round_github_request_ids(conn, round_["round_id"]))
    if cause["reason"] in GITHUB_WITHDRAWAL_REASONS:
        return mixed and any(
            source_members.matches_disposition_fact(member, cause, accepted_at=root["accepted_at"])
            for member in source_members.disposition_members(conn, root, round_)
        )
    if mixed:
        return (
            cause["reason"] in {"comment_deleted", "mention_removed"}
            and isinstance(cause.get("version"), str)
            and bool(cause["version"])
            and any(
                member.ownership.request["auth_type"] == "tapd_mention"
                and member.member["request_id"] == cause["request_id"]
                and any(event["event_id"] == cause["event_id"] for event in member.ownership.events)
                for member in source_members.disposition_members(conn, root, round_)
            )
        )
    return (
        cause["reason"] in {"comment_deleted", "mention_removed"}
        and isinstance(cause["version"], str)
        and bool(cause["version"])
        and any(
            (member["request_id"], member["event_id"], member["auth_type"])
            == (cause["request_id"], cause["event_id"], "tapd_mention")
            for member in source_results.consumed_members(conn, root, round_)
        )
    )


def _verified(conn, root_id, row):
    proof = json_object(row["proof_json"])
    root = source_results.by_id(conn, root_id)
    if proof is None or root is None or utc_timestamp(row["retired_at"]) is None:
        return False
    round_ = source_results.delivery_round(conn, root["source_id"], root["round_id"])
    cancellation = tapd_intake.round_cancellation(conn, root["round_id"])
    if (
        round_ is None
        or round_["status"] != SourceRoundStatus.CANCELED
        or round_["finished_at"] != proof["round_finished_at"]
        or proof["version"] != 1
        or proof["revision"] != row["observation_revision"]
        or not re.fullmatch(r"[0-9a-f]{64}", row["observation_revision"])
        or not _valid_cause(conn, root, round_, proof["cause"])
        or proof["commitment"] != commitment(conn, root, round_, cancellation)
    ):
        return False
    pause = source_delivery_pauses.latest(conn, root_id)
    if proof["pause_sha256"] != (context_digest(dict(pause)) if pause is not None else None):
        return False
    owner = source_delivery_recovery.for_generation(conn, root_id, pause["generation"]) if pause is not None else None
    if proof["transition_id"] != (owner["transition_id"] if owner else None) or (
        owner is not None and (owner["completed_at"] is not None or owner["suspended_at"] != row["retired_at"])
    ):
        return False
    rows = source_results.required_actions(conn, root_id)
    first_missing = proof["first_missing"]
    if type(first_missing) is not int or not 0 <= first_missing <= len(rows):
        return False
    original = _original_actions(rows, root_id, first_missing, row["retired_at"])
    return (
        original is not None
        and action_digests(original) == proof["action_sha256"]
        and action_plan(conn, root, original, proof["commitment"]["target"]) == first_missing
    )
