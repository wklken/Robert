"""Exact immutable evidence for an accepted but dormant source delivery."""

import re
from hashlib import sha256

from sqlalchemy import func, or_, select

from robert_agent.foundation.contracts.delivery import (
    AuditStatus,
    PublicationStatus,
    PublishStatus,
    SourceControllerAction,
    SourceResultKind,
)
from robert_agent.foundation.contracts.execution import AttemptStatus
from robert_agent.foundation.contracts.work import SourceCapability, SourceRoundStatus, TaskLifecycle
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries import (
    source_feedback,
    source_github_publication,
    source_members,
    source_results,
    source_tasks,
)
from robert_agent.persistence.rounds.source_evidence import context_digest, json_object


def _latest():
    table = tables["source_delivery_pauses"]
    newer = table.alias("newer_delivery_pause")
    return select(table).where(
        table.c.generation
        == select(func.max(newer.c.generation))
        .where(newer.c.source_result_id == table.c.source_result_id)
        .correlate(table)
        .scalar_subquery()
    )


def latest(conn, source_result_id):
    table = tables["source_delivery_pauses"]
    return records.first(statements.read(conn, _latest().where(table.c.source_result_id == source_result_id)))


def latest_in_scope(conn, round_ids, source_id):
    table = tables["source_delivery_pauses"]
    scope = table.c.round_id.in_(round_ids) if source_id is None else table.c.source_id == source_id
    return statements.read(conn, _latest().where(scope).order_by(table.c.source_result_id))


def _all_round_actions(conn, root):
    action, member = (tables[name] for name in ("github_actions", "source_round_tasks"))
    return statements.read(
        conn,
        select(action.c.action_id)
        .select_from(action.outerjoin(member, member.c.task_id == action.c.task_id))
        .where(or_(member.c.round_id == root["round_id"], action.c.source_result_id == root["source_result_id"])),
    )


def _prepared_tapd(action, target):
    packet = json_object(action["prepared_evidence_json"])
    return (
        packet is not None
        and packet.get("action_id") == action["action_id"]
        and packet.get("target") == target
        and packet.get("normalization_version") == 1
        and isinstance(packet.get("receipt_id"), str)
        and isinstance(packet.get("body"), str)
        and bool(packet["receipt_id"])
        and packet["receipt_id"] in packet["body"]
        and context_digest(packet) == action["intent_fingerprint"]
    )


def _candidate_metadata(action):
    metadata = json_object(action["metadata_json"])
    if metadata is None:
        return None
    diagnostic = metadata.get("publish")
    if (
        isinstance(diagnostic, dict)
        and diagnostic.get("status") in {"deferred", "attention"}
        and set(diagnostic) <= {"status", "reason_code", "retryable", "next_retry_at", "failed_at"}
    ):
        return {key: value for key, value in metadata.items() if key != "publish"}
    return metadata


def locally_never_started(conn, root, action, target):
    metadata = _candidate_metadata(action)
    if (
        metadata is None
        or "publish" in metadata
        or action["publish_status"] != PublishStatus.NOT_PUBLISHED
        or any(
            action[key] is not None
            for key in ("external_id", "target_url", "intent_started_at", "intent_finished_at", "intent_error_json")
        )
    ):
        return False
    if action["publication_id"] is None:
        return action["intent_status"] is None and action["remote_evidence_json"] is None
    if (
        action["intent_status"] != PublicationStatus.PREPARED
        or json_object(action["remote_evidence_json"]) != {}
        or utc_timestamp(action["intent_created_at"]) is None
        or action["intent_created_at"] != action["intent_updated_at"]
    ):
        return False
    if action["action_type"] == SourceControllerAction.GITHUB_FEEDBACK:
        saved = source_feedback.required_action(conn, root["source_result_id"], action["action_id"])
        return (
            saved is not None
            and source_feedback.prepared_request(conn, saved) is not None
            and action["prepared_evidence_json"] is None
        )
    if action["provider"] == "github":
        saved = source_github_publication.required_action(conn, root["source_result_id"], action["action_id"])
        return (
            saved is not None
            and source_github_publication.prepared_request(saved) is not None
            and action["prepared_evidence_json"] is None
        )
    return action["github_prepared_evidence_json"] is None and _prepared_tapd(action, target)


def effect_digest(action):
    return context_digest({**dict(action), "metadata_json": _candidate_metadata(action)})


def candidate_digest(action):
    return context_digest(
        {
            **{
                key: action[key]
                for key in (
                    "action_id",
                    "task_id",
                    "source_result_id",
                    "result_id",
                    "action_type",
                    "provider",
                    "target_json",
                    "audit_status",
                    "created_at",
                    "position",
                )
            },
            "metadata": _candidate_metadata(action),
        }
    )


def exact_action_set(conn, root, actions):
    return {row["action_id"] for row in actions} == {row[0] for row in _all_round_actions(conn, root)}


def _effect_proof(conn, root, actions, target):
    sets = source_results.required_action_sets(conn, root, actions)
    if sets is None or not exact_action_set(conn, root, actions):
        return None
    base, _feedback = sets
    if not base or base[-1]["provider"] != "tapd":
        return None
    first_missing = next(
        (i for i, row in enumerate(actions) if row["publish_status"] != PublishStatus.PUBLISHED), len(actions)
    )
    confirmed = source_results.confirmed_required_effects(
        conn, root["source_result_id"], target, before_position=first_missing if first_missing < len(actions) else None
    )
    if confirmed is None:
        return None
    proof = []
    for position, action in enumerate(actions):
        if (
            action["position"] != position
            or action["provider"] not in {"tapd", "github"}
            or action["audit_status"] != AuditStatus.ACCEPTED
            or (position >= first_missing and not locally_never_started(conn, root, action, target))
        ):
            return None
        proof.append(
            {
                "action_id": action["action_id"],
                "publication_id": action["publication_id"],
                "disposition": "confirmed" if position < first_missing else "local_never_started",
                "sha256": effect_digest(action),
            }
        )
    return proof


def materialized_proof(conn, root, round_, cancellation, scope, *, revision, execution_finished_at=None):
    """Rebuild the whole audit, frozen membership and effect proof for one root."""
    if root is None or round_ is None or cancellation is None or re.fullmatch(r"[0-9a-f]{64}", revision) is None:
        return None
    accepted, requested = map(utc_timestamp, (root["accepted_at"], cancellation["requested_at"]))
    evidence = json_object(root["evidence_json"]) or {}
    source = source_results.tapd_source(conn, root["source_id"])
    worker = source_results.worker_candidate(conn, root["worker_result_id"]) if root["worker_result_id"] else None
    if (
        source is None
        or worker is None
        or worker["attempt_status"] != AttemptStatus.COMPLETED
        or worker["lifecycle"] != TaskLifecycle.COMPLETED
        or (worker["attempt_id"], worker["task_id"]) != (evidence.get("attempt_id"), evidence.get("task_id"))
        or root["kind"] not in (SourceResultKind.ANSWER, SourceResultKind.QUESTION)
        or round_["capability"] != SourceCapability.DEVELOPMENT
        or root["round_id"] != round_["round_id"]
        or root["source_id"] != round_["source_id"]
        or root["input_version"] != round_["input_version"]
        or round_["input_sha256"] != evidence.get("round_input_sha256")
        or round_["input_sha256"] != sha256(round_["input_snapshot_json"].encode()).hexdigest()
        or cancellation["round_id"] != root["round_id"]
        or cancellation["reason"] not in {"confirmation_required", "github_development_binding_paused"}
        or accepted is None
        or requested is None
        or requested < accepted
    ):
        return None
    repository = scope.get("repository")
    binding = scope.get("binding")
    rule = binding.get("rule") if isinstance(binding, dict) else None
    if (
        not isinstance(repository, dict)
        or not isinstance(rule, dict)
        or binding.get("state") != "paused"
        or binding.get("reason") != "no_label_match"
        or repository.get("full_name") != rule.get("repo")
        or repository.get("repo_id") != evidence.get("repo_id")
    ):
        return None
    current_repo = source_tasks.repository(conn, repository["repo_id"])
    if current_repo is None or any(
        current_repo[key] != repository[key]
        for key in ("repo_id", "full_name", "repo_root", "worktree_root", "default_base_branch", "github_account")
    ):
        return None
    github = source_members.round_github_request_ids(conn, round_["round_id"])
    if github:
        members = source_members.disposition_consumed_evidence(conn, root, round_)
        original = tuple(
            item for item in source_members.disposition_members(conn, root, round_) if item.ownership.return_target
        )
        intact = scope.get("github_disposition")
        if (
            not isinstance(intact, list)
            or len(intact) != len(github)
            or len(original) != len(github)
            or any(
                not isinstance(fact, dict)
                or fact.get("reason") != "github_current_mention"
                or not source_members.matches_disposition_fact(member, fact, accepted_at=root["accepted_at"])
                for member, fact in zip(original, intact, strict=True)
            )
        ):
            return None
    else:
        if cancellation["reason"] != "confirmation_required" or "github_disposition" in scope:
            return None
        members = source_results.consumed_members(conn, root, round_)
    actions = source_results.required_actions(conn, root["source_result_id"])
    target = source_results.source_target(source)
    sets = source_results.required_action_sets(conn, root, actions)
    effects = _effect_proof(conn, root, actions, target)
    if (
        not members
        or effects is None
        or sets is None
        or any(action["task_id"] != worker["task_id"] for action in sets[0])
    ):
        return None
    return {
        "version": 1,
        **({"execution_finished_at": execution_finished_at} if execution_finished_at is not None else {}),
        "scope": scope,
        "observation_revision": revision,
        "target": target,
        "cancellation": dict(cancellation),
        "round": {key: round_[key] for key in round_.keys() if key not in ("status", "finished_at")},
        "root_sha256": context_digest(dict(root)),
        "worker_result_sha256": context_digest(dict(worker)),
        "consumed": [dict(member) for member in members],
        "effects": effects,
    }


def dormant_actions(conn, pause):
    """Return precisely the saved never-started actions, or refuse stale evidence."""
    if latest(conn, pause["source_result_id"]) != pause:
        return None
    root = source_results.for_source(conn, pause["source_id"], pause["source_result_id"])
    round_ = source_results.delivery_round(conn, pause["source_id"], pause["round_id"])
    saved = json_object(pause["proof_json"])
    if (
        root is None
        or round_ is None
        or root["round_id"] != pause["round_id"]
        or round_["status"] not in (SourceRoundStatus.ACTIVE, SourceRoundStatus.CANCELED)
        or saved is None
        or (
            round_["status"] == SourceRoundStatus.CANCELED
            and round_["finished_at"] != saved.get("execution_finished_at", pause["paused_at"])
        )
        or not isinstance(saved.get("scope"), dict)
    ):
        return None
    table = tables["source_round_cancellations"]
    cancellation = records.first(statements.read(conn, select(table).where(table.c.round_id == pause["round_id"])))
    try:
        actual = materialized_proof(
            conn,
            root,
            round_,
            cancellation,
            saved["scope"],
            revision=pause["observation_revision"],
            execution_finished_at=saved.get("execution_finished_at"),
        )
    except (IndexError, KeyError, TypeError, ValueError, RecursionError):
        return None
    paused, requested = (
        (utc_timestamp(pause["paused_at"]), utc_timestamp(cancellation["requested_at"]))
        if cancellation
        else (None, None)
    )
    if actual is None or actual != saved or paused is None or requested is None or paused < requested:
        return None
    return tuple(row["action_id"] for row in actual["effects"] if row["disposition"] == "local_never_started")


def is_dormant(conn, source_result_id):
    pause = latest(conn, source_result_id)
    return pause is not None and dormant_actions(conn, pause) is not None
