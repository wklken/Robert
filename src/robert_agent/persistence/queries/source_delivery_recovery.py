"""Load the exact owner and frozen proof for historical source delivery."""

import re
from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import func, select

from robert_agent.foundation.contracts.delivery import PublishStatus
from robert_agent.foundation.contracts.work import SourceRoundStatus
from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.expressions import valid_json_or_empty
from robert_agent.persistence.queries import (
    artifacts,
    source_confirmation,
    source_delivery_pauses,
    source_members,
    source_results,
    source_tasks,
    tapd_intake,
)
from robert_agent.persistence.rounds.source_evidence import context_digest, json_object


def active(conn, source_id):
    table = tables["source_delivery_recoveries"]
    return records.first(
        statements.read(
            conn,
            select(table).where(
                table.c.source_id == source_id, table.c.completed_at.is_(None), table.c.suspended_at.is_(None)
            ),
        )
    )


def for_generation(conn, result_id, generation):
    table = tables["source_delivery_recoveries"]
    return records.first(
        statements.read(
            conn, select(table).where(table.c.source_result_id == result_id, table.c.generation == generation)
        )
    )


@dataclass(frozen=True, slots=True)
class BoundDelivery:
    digest: str
    missing: tuple[str, ...]
    prompt_path: str
    prompt_sha256: str


def bound_delivery(conn, pause):
    """Compare immutable roots and original receipts while permitting only saved missing effects to advance."""
    try:
        return _bound_delivery(conn, pause)
    except (KeyError, IndexError, TypeError, ValueError, RecursionError):
        return None


def _bound_delivery(conn, pause):
    saved = json_object(pause["proof_json"])
    source_id, root_id, round_id = (pause[k] for k in ("source_id", "source_result_id", "round_id"))
    root = source_results.for_source(conn, source_id, root_id)
    round_ = source_results.delivery_round(conn, source_id, round_id)
    if (
        saved is None
        or root is None
        or round_ is None
        or root["round_id"] != round_id
        or round_["status"] != SourceRoundStatus.CANCELED
        or round_["finished_at"] != saved.get("execution_finished_at", pause["paused_at"])
        or context_digest(dict(root)) != saved.get("root_sha256")
        or sha256(round_["input_snapshot_json"].encode()).hexdigest() != round_["input_sha256"]
        or saved.get("round") != {key: round_[key] for key in round_.keys() if key not in ("status", "finished_at")}
    ):
        return None
    mixed = bool(source_members.round_github_request_ids(conn, round_id))
    consumed = (
        source_members.disposition_consumed_evidence(conn, root, round_)
        if mixed
        else source_results.consumed_members(conn, root, round_)
    )
    cancellation = tapd_intake.round_cancellation(conn, round_id)
    worker = source_results.worker_candidate(conn, root["worker_result_id"])
    if (
        cancellation is None
        or dict(cancellation) != saved.get("cancellation")
        or worker is None
        or context_digest(dict(worker)) != saved.get("worker_result_sha256")
        or not consumed
        or [dict(member) for member in consumed] != saved.get("consumed")
    ):
        return None
    source = source_results.tapd_source(conn, source_id)
    if source is None or source_results.source_target(source) != saved.get("target"):
        return None
    repo = saved["scope"]["repository"]
    current_repo = source_tasks.repository(conn, repo["repo_id"])
    if current_repo is None or any(current_repo[key] != value for key, value in repo.items()):
        return None
    actions = source_results.required_actions(conn, root_id)
    effects = saved["effects"]
    if len(actions) != len(effects) or not actions or not source_delivery_pauses.exact_action_set(conn, root, actions):
        return None
    if mixed:
        first_pending = next(
            (index for index, action in enumerate(actions) if action["publish_status"] != PublishStatus.PUBLISHED),
            len(actions),
        )
        if source_results.confirmed_required_effects(
            conn,
            root_id,
            saved["target"],
            before_position=first_pending if first_pending < len(actions) else None,
        ) is None or any(action["publish_status"] != PublishStatus.NOT_PUBLISHED for action in actions[first_pending:]):
            return None
    missing = []
    for action, effect in zip(actions, effects, strict=True):
        if action["action_id"] != effect["action_id"]:
            return None
        if effect["disposition"] == "confirmed":
            if source_delivery_pauses.effect_digest(action) != effect["sha256"]:
                return None
        elif effect["disposition"] == "local_never_started":
            missing.append(action["action_id"])
        else:
            return None
    attempt = source_tasks.attempt(conn, worker["task_id"], worker["attempt_id"])
    metadata = json_object(attempt["metadata_json"]) if attempt is not None else None
    prompt = metadata["prompt_contract"]["prompt_sha256"] if metadata else None
    paths = [
        row["path"]
        for row in artifacts.find_artifact_id_and_artifact_type_and_path_and_bytes_by_attempt_id(
            conn, worker["attempt_id"]
        )
        if row["artifact_type"] == "prompt"
    ]
    if not isinstance(prompt, str) or len(paths) != 1:
        return None
    digest = context_digest(
        {
            "pause": dict(pause),
            "candidates": [source_delivery_pauses.candidate_digest(row) for row in actions],
            "prompt": [paths[0], prompt],
            "workspace": metadata.get("native_workspace"),
        }
    )
    return BoundDelivery(digest, tuple(missing), paths[0], prompt)


def current_bound(conn, owner):
    pause = source_delivery_pauses.latest(conn, owner["source_result_id"])
    if owner["suspended_at"] is not None:
        return None
    if pause is None or pause["generation"] != owner["generation"] or pause["source_id"] != owner["source_id"]:
        return None
    proof = bound_delivery(conn, pause)
    return proof if proof is not None and proof.digest == owner["binding_sha256"] else None


def applied_bound(conn, owner):
    """Require the exact claimed confirmation transition in addition to the accepted result."""
    bound = current_bound(conn, owner)
    if bound is None:
        return None
    pause = source_delivery_pauses.latest(conn, owner["source_result_id"])
    transition = source_confirmation.resume_transition(conn, owner["source_id"], owner["transition_id"])
    answer = source_confirmation.answer(conn, owner["question_id"])
    scope = (json_object(pause["proof_json"]) or {}).get("scope")
    if (
        transition is None
        or answer is None
        or not isinstance(scope, dict)
        or transition["question_id"] != owner["question_id"]
        or transition["applied_at"] != owner["claimed_at"]
        or answer["claimed_at"] != owner["claimed_at"]
        or answer["consumer_id"] != owner["transition_id"]
        or json_object(transition["paused_binding_json"]) != scope.get("binding")
        or json_object(transition["repository_json"]) != scope.get("repository")
    ):
        return None
    if source_members.round_github_request_ids(conn, pause["round_id"]):
        root = source_results.for_source(conn, owner["source_id"], owner["source_result_id"])
        round_ = source_results.delivery_round(conn, owner["source_id"], pause["round_id"])
        if root is None or round_ is None or not _applied_original_discussions(conn, root, round_, owner):
            return None
    return bound


def _applied_original_discussions(conn, root, round_, owner):
    evidence = json_object(owner["evidence_json"]) or {}
    github = evidence.get("original_github")
    if (
        not isinstance(github, dict)
        or github.get("version") != 1
        or not isinstance(github.get("scope_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", github["scope_sha256"]) is None
        or not isinstance(github.get("members"), list)
    ):
        return False
    originals = [
        member
        for member in source_members.disposition_members(conn, root, round_)
        if member.ownership.return_target is not None
    ]
    return (
        bool(originals)
        and len(originals) == len(github["members"])
        and all(
            isinstance(fact, dict)
            and fact.get("reason") == "github_current_mention"
            and source_members.matches_disposition_fact(member, fact, accepted_at=root["accepted_at"])
            for member, fact in zip(originals, github["members"], strict=True)
        )
    )


def projection_is_current(conn, root, round_, repo_id):
    """Only actual native preparation transfers the reusable WorkItem projection."""
    item = tables["work_items"]
    return bool(
        statements.read(
            conn,
            select(item.c.work_item_id).where(
                item.c.origin_type == "tapd",
                item.c.origin_source_id == root["source_id"],
                item.c.repo_id == repo_id,
                func.json_extract(valid_json_or_empty(item.c.metadata_json), "$.native_projection_round_id")
                == round_["round_id"],
            ),
        )
    )


def completed(conn, root, target):
    pause = source_delivery_pauses.latest(conn, root["source_result_id"])
    owner = for_generation(conn, root["source_result_id"], pause["generation"]) if pause is not None else None
    return bool(
        owner is not None
        and owner["completed_at"] is not None
        and owner["source_id"] == root["source_id"]
        and applied_bound(conn, owner) is not None
        and source_results.confirmed_required_effects(conn, root["source_result_id"], target)
    )
