"""Conclude a native source round after its exact required receipts are confirmed."""

from dataclasses import dataclass
from hashlib import sha256

from robert_agent.delivery.application import native_preparation, source_delivery_observation, source_pr_lineage
from robert_agent.delivery.application import source_results as authority
from robert_agent.delivery.native_contract import selectors_for
from robert_agent.delivery.tapd_policy import prepare_comment
from robert_agent.foundation.contracts.delivery import OutputType, SourceResultKind
from robert_agent.foundation.contracts.work import SourceRoundStatus, WorkstreamLifecycle
from robert_agent.persistence import records, transactions
from robert_agent.persistence.queries import (
    source_delivery_pauses,
    source_delivery_recovery,
    source_members,
    tapd_intake,
)
from robert_agent.persistence.queries import (
    source_results as read,
)
from robert_agent.persistence.queries import (
    source_sessions as sessions,
)
from robert_agent.persistence.queries import work_items as items
from robert_agent.persistence.rounds.source_evidence import json_object
from robert_agent.persistence.stores import source_delivery_recovery as recovery_write
from robert_agent.persistence.stores import source_sessions as fence
from robert_agent.persistence.stores import tapd_intake as intake_write
from robert_agent.persistence.stores import work_items as write_items
from robert_agent.persistence.stores import workstreams
from robert_agent.work import source_attention
from robert_agent.work import tapd_authority as facts
from robert_agent.work.application import tapd_intake as current_intake
from robert_agent.work.application.commands import record_system_event
from robert_agent.work.application.tapd_intake import current_delivery_transition


@dataclass(frozen=True, slots=True)
class CompletionOutcome:
    status: str
    reason: str


def _result_actions(conn, root, target):
    actions = read.confirmed_required_effects(conn, root["source_result_id"], target)
    sets = read.required_action_sets(conn, root, actions) if actions else None
    if sets is None or any(not authority.required_effect(root, action, target) for action in actions):
        return ()
    base, _feedback = sets
    if base[-1]["provider"] != "tapd":
        return ()
    if root["worker_result_id"] is not None:
        candidate = read.worker_candidate(conn, root["worker_result_id"])
        payload = json_object(root["payload_json"]) or {}
        output = payload.get("output_type")
        if (
            candidate is None
            or candidate["output_type"] != output
            or root["kind"]
            != (SourceResultKind.QUESTION if output == OutputType.WAITING_FOR_USER else SourceResultKind.ANSWER)
            or output
            not in {
                OutputType.WAITING_FOR_USER,
                OutputType.COMMENT_ANALYSIS,
                OutputType.NEW_PR,
                OutputType.UPDATE_EXISTING_PR,
            }
        ):
            return ()
        metadata = json_object(candidate["metadata_json"]) or {}
        audit = metadata.get("audit")
        if not isinstance(audit, dict) or audit.get("planned_actions") != selectors_for(output):
            return ()
        selectors = [
            {
                "provider": row["provider"],
                "type": row["action_type"],
                "target_id": (json_object(row["metadata_json"]) or {}).get("target_id"),
            }
            for row in base
        ]
        if audit["planned_actions"] != selectors:
            return ()
        body = native_preparation.delivered_worker_tapd_body(
            conn, root["source_result_id"], base[-1], metadata, candidate["output_type"], base[:-1]
        )
    else:
        if len(base) != 1:
            return ()
        body = (json_object(root["payload_json"]) or {}).get("body")
    if not isinstance(body, str):
        return ()
    prepared = json_object(base[-1]["prepared_evidence_json"])
    try:
        rendered = prepare_comment(
            body, prepared["receipt_id"], bot_name=prepared["bot_name"], reply_name=prepared["reply_name"]
        )
    except ValueError:
        return ()
    if rendered.body != prepared["body"] or rendered.body_digest != prepared["body_digest"]:
        return ()
    return base[-1], body, actions


def _work_item(conn, source_id, repo_id):
    item = records.first(items.find_tapd_work_item(conn, source_id=source_id, repo_id=repo_id))
    if item is None or item["completed_at"] is not None or item["canceled_at"] is not None:
        return None
    stream = records.first(items.find_tapd_work_item_stream(conn, item["workstream_id"]))
    if (
        stream is None
        or stream["primary_source_id"] != source_id
        or stream["repo_id"] != repo_id
        or stream["origin_workstream_id"] is not None
        or stream["lifecycle"] == WorkstreamLifecycle.CANCELED
    ):
        return None
    return item


def _current_completion(conn, config, ticket, claim, root, round_, action, observation, revision, now):
    if not source_delivery_observation.matches_completion(
        conn, observation, ticket, claim, root["source_result_id"], revision=revision
    ):
        return False
    members = source_members.accepted_round_members(conn, root, round_)
    if not members:
        return False
    checked = current_intake.current_accepted_github_completion(conn, config, ticket, observation.github, now=now)
    if checked is None:
        return False
    tapd_members = tuple(member.ownership.request for member in members if member.ownership.return_target is None)
    tapd = config["tapd"]
    decisions = current_intake.delivery_request_decisions(conn, tapd, ticket, tapd_members, revision=revision)
    cancellation = checked.cancellation or current_intake.completion_tapd_cancellation(
        conn,
        tapd,
        ticket,
        tapd_members,
        decisions,
        revision=revision,
    )
    if cancellation:
        intake_write.request_cancellation(
            conn, source_id=ticket.source_id, round_id=round_["round_id"], reason=cancellation, now=now
        )
        return False
    repositories, repo_id = authority.accepted_delivery_lineage(conn, config, root, round_, action)
    return (
        repositories is not None
        and set(checked.decisions) == set(observation.github.scope.request_ids)
        and len(decisions) == len(tapd_members)
        and all(
            decision.reason == "allowed"
            and decision.capability == round_["capability"]
            and repositories <= set(decision.repos)
            for decision in (*checked.decisions.values(), *decisions)
        )
        and all(row["repo_id"] in (None, repo_id) for row in tapd_members)
        and source_delivery_observation.is_current(observation)
    )


def complete_source_result(conn, config, claim, *, source_result_id, now, completion_observation=None):
    """Finish only the accepted current round, without replaying delivery or consuming input again."""
    if not conn.in_transaction or not fence.lock_claim(conn, claim, now=now):
        return CompletionOutcome("held", "source_fence_unavailable")
    root = read.for_source(conn, claim.source_id, source_result_id)
    source = read.tapd_source(conn, claim.source_id) if root else None
    if (
        source is None
        or not root["round_id"]
        or root["kind"] not in (SourceResultKind.ANSWER, SourceResultKind.QUESTION)
    ):
        return CompletionOutcome("ineligible", "source_round_unavailable")
    try:
        ticket = facts.TicketIdentity(
            source["host"], source["project_key"], source["source_type"], source["external_id"]
        )
    except (TypeError, ValueError):
        return CompletionOutcome("ineligible", "source_identity_invalid")
    if ticket.source_id != root["source_id"]:
        return CompletionOutcome("ineligible", "source_identity_invalid")
    round_ = read.delivery_round(conn, ticket.source_id, root["round_id"])
    if round_ is None:
        return CompletionOutcome("ineligible", "source_round_unavailable")
    question = root["kind"] == SourceResultKind.QUESTION
    disposition = SourceRoundStatus.WAITING_FOR_USER if question else SourceRoundStatus.EXECUTION_FINISHED
    owner = None
    if round_["status"] == SourceRoundStatus.CANCELED:
        pause = source_delivery_pauses.latest(conn, source_result_id)
        if pause is not None:
            owner = source_delivery_recovery.for_generation(conn, source_result_id, pause["generation"])
            if owner is not None and source_delivery_recovery.applied_bound(conn, owner) is None:
                return CompletionOutcome("held", "source_delivery_recovery_changed")
    if owner is None and (
        round_["status"] not in (SourceRoundStatus.ACTIVE, disposition)
        or tapd_intake.round_cancellation(conn, round_["round_id"])
    ):
        return CompletionOutcome("held", "source_round_canceled_or_inactive")
    evidence = json_object(root["evidence_json"]) or {}
    target = read.source_target(source)
    if (
        json_object(round_["publication_scope_json"]) != {**target, "action": "comment"}
        or root["input_version"] != round_["input_version"]
        or evidence.get("round_input_sha256") != round_["input_sha256"]
        or sha256(round_["input_snapshot_json"].encode()).hexdigest() != round_["input_sha256"]
    ):
        return CompletionOutcome("ineligible", "source_scope_changed")
    delivery = _result_actions(conn, root, target)
    if not delivery:
        return CompletionOutcome("held", "required_receipts_unavailable")
    mixed = bool(source_members.round_github_request_ids(conn, root["round_id"]))
    if mixed and owner is not None:
        return CompletionOutcome("held", "mixed_delivery_recovery_unavailable")
    tapd_action, body, receipts = delivery
    if not source_pr_lineage.ensure_root_pr_lineage(conn, claim, source_result_id=source_result_id, now=now):
        return CompletionOutcome("held", "source_pr_lineage_unavailable")
    if round_["status"] == disposition or (owner is not None and owner["completed_at"] is not None):
        return CompletionOutcome("already_completed", "source_round_already_completed")
    blockers = (
        sessions.blockers_for_publication(conn, ticket.source_id, source_result_id)
        if owner is not None
        else sessions.blockers_for_source(conn, ticket.source_id, limit=1)
    )
    if blockers or read.unknown_github_effect(conn, ticket.source_id, require_positive_receipt=True):
        return CompletionOutcome("held", "source_busy")
    revision = (json_object(source["metadata_json"]) or {}).get("revision")
    if mixed:
        if not _current_completion(
            conn, config, ticket, claim, root, round_, tapd_action, completion_observation, revision, now
        ):
            return CompletionOutcome("held", "source_delivery_scope_changed")
    elif completion_observation is not None or (
        owner is None
        and not authority.current_delivery_requests(conn, config, ticket, root, round_, tapd_action, revision=revision)
    ):
        return CompletionOutcome("held", "source_delivery_scope_changed")
    item = None
    repo_id = evidence.get("repo_id")
    if root["worker_result_id"] is not None and (
        owner is None
        or (
            current_delivery_transition(conn, config, ticket, revision=revision) == owner["transition_id"]
            and source_delivery_recovery.projection_is_current(conn, root, round_, repo_id)
        )
    ):
        item = _work_item(conn, ticket.source_id, repo_id)
        if item is None and owner is None:
            return CompletionOutcome("held", "source_work_item_unavailable")

    checkpoint = "complete_native_source"
    transactions.savepoint(conn, checkpoint)
    try:
        closed = (
            recovery_write.complete(conn, owner, now=now)
            if owner is not None
            else fence.finish_round(conn, claim, round_id=round_["round_id"], outcome=disposition, now=now)
        )
        if not closed:
            transactions.finish_isolation(conn, checkpoint, rollback=True)
            return CompletionOutcome("held", "source_busy")
        if owner is not None:
            source_attention.resolve_delivery_recovery(
                conn, ticket.source_id, source_result_id, owner["generation"], now=now
            )
        if item is not None:
            scope = {"source_id": ticket.source_id, "repo_id": item["repo_id"], "now": now}
            if not (
                write_items.mark_tapd_delivery(conn, work_item_id=item["work_item_id"], completed=not question, **scope)
                and workstreams.set_tapd_control_lifecycle(
                    conn,
                    workstream_id=item["workstream_id"],
                    **scope,
                    lifecycle=WorkstreamLifecycle.WAITING_FOR_USER if question else WorkstreamLifecycle.COMPLETED,
                )
            ):
                transactions.finish_isolation(conn, checkpoint, rollback=True)
                return CompletionOutcome("held", "source_work_item_changed")
            record_system_event(
                conn,
                item["work_item_id"],
                event_type="tapd_question_delivered" if question else "completed",
                idempotency_key=f"tapd-delivered:{source_result_id}",
                body="TAPD question delivered." if question else "TAPD delivery confirmed.",
                metadata={
                    "source_result_id": source_result_id,
                    "source_id": ticket.source_id,
                    "task_id": evidence["task_id"],
                    "round_id": round_["round_id"],
                },
                now=now,
            )
        if question:
            source_attention.open_delivered_question(
                conn,
                root,
                tapd_action,
                body=body,
                now=now,
                receipts=receipts if mixed or len(receipts) != 1 else None,
            )
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.release(conn, checkpoint)
    return CompletionOutcome("completed", disposition.value)
