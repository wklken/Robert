"""Settle accepted delivery locally after authoritative permanent source revocation."""

from robert_agent.delivery.application import source_delivery_pause
from robert_agent.delivery.application import source_results as authority
from robert_agent.delivery.tapd_policy import prepare_comment
from robert_agent.foundation.contracts.work import SourceRoundStatus
from robert_agent.persistence import records, transactions
from robert_agent.persistence.queries import (
    source_delivery_pauses,
    source_delivery_recovery,
    source_members,
    source_results,
    source_sessions,
    work_items,
)
from robert_agent.persistence.queries import (
    source_retirement as read,
)
from robert_agent.persistence.queries import (
    tapd_intake as intake,
)
from robert_agent.persistence.rounds.source_evidence import context_digest, json_object
from robert_agent.persistence.stores import (
    source_delivery_recovery as recovery,
)
from robert_agent.persistence.stores import (
    source_retirement as write,
)
from robert_agent.persistence.stores import (
    source_sessions as fence,
)
from robert_agent.work import source_attention
from robert_agent.work.application import source_request_authority, tapd_intake
from robert_agent.work.application.commands import record_system_event


def _model_comment_matches(conn, config, root, round_, actions):
    sets = source_results.required_action_sets(conn, root, actions)
    if sets is None or len(sets[0]) != 1:
        return False
    base, feedback = sets
    if any(authority.accepted_delivery_lineage(conn, config, root, round_, row)[0] is None for row in feedback):
        return False
    action = base[0]
    if authority.accepted_delivery_lineage(conn, config, root, round_, action)[0] is None:
        return False
    if action["prepared_evidence_json"] is None:
        return True
    saved = json_object(action["prepared_evidence_json"])
    body = (json_object(root["payload_json"]) or {}).get("body")
    packet = prepare_comment(body, saved["receipt_id"], bot_name=saved["bot_name"], reply_name=saved["reply_name"])
    return all(
        saved.get(key) == getattr(packet, key) for key in ("body", "body_digest", "normalization_version", "receipt_id")
    )


def _projection(conn, root, round_, now):
    if root["worker_result_id"] is None:
        return
    evidence = json_object(root["evidence_json"]) or {}
    source_id, repo_id, round_id = root["source_id"], evidence["repo_id"], round_["round_id"]
    if not source_delivery_recovery.projection_is_current(conn, root, round_, repo_id):
        return
    item = records.first(work_items.find_tapd_work_item(conn, source_id=source_id, repo_id=repo_id))
    if item is None or not write.touch_projection(
        conn, source_id=source_id, repo_id=repo_id, round_id=round_id, item_id=item["work_item_id"], now=now
    ):
        return
    record_system_event(
        conn,
        item["work_item_id"],
        event_type="tapd_delivery_retired",
        idempotency_key="tapd-retired:" + root["source_result_id"],
        body="Remaining TAPD delivery canceled after permanent revocation.",
        metadata={"source_id": source_id, "source_result_id": root["source_result_id"], "round_id": round_id},
        now=now,
    )


def retire_accepted(conn, config, ticket, claim, *, round_id, revision, now, disposition=None):
    """Retire only an exact canceled result with a settled prefix and unstarted suffix."""
    if (
        not conn.in_transaction
        or not revision
        or claim.source_id != ticket.source_id
        or not fence.lock_claim(conn, claim, now=now)
        or source_sessions.execution_blocked_for_source(conn, claim.source_id)
    ):
        return None
    root = source_results.for_round(conn, round_id)
    round_ = source_results.delivery_round(conn, ticket.source_id, round_id)
    cancellation = intake.round_cancellation(conn, round_id)
    if (
        root is None
        or round_ is None
        or cancellation is None
        or round_["status"] not in (SourceRoundStatus.ACTIVE, SourceRoundStatus.CANCELED)
        or read.for_root(conn, root["source_result_id"]) is not None
    ):
        return None
    root_id = root["source_result_id"]
    try:
        commitment = read.commitment(conn, root, round_, cancellation)
        if commitment is None:
            return None
        if source_members.round_github_request_ids(conn, round_id):
            if (
                source_results.unknown_github_effect(conn, ticket.source_id, require_positive_receipt=True)
                or source_results.tapd_publication_evidence(conn, ticket.source_id).unknown_intents
            ):
                return None
            cause = tapd_intake.mixed_permanent_tapd_cause(
                conn, config.get("tapd") or {}, ticket, root, round_, revision=revision
            )
            checked = source_request_authority.current_disposition(
                conn,
                config,
                ticket,
                claim,
                root_id,
                disposition,
                revision=revision,
                now=now,
            )
            if cause is None and checked is not None and checked.revoked:
                cause = checked.revoked[0]
        else:
            checked = None
            cause = tapd_intake.permanent_delivery_cause(
                conn,
                config.get("tapd") or {},
                ticket,
                source_results.consumed_members(conn, root, round_),
                revision=revision,
            )
        if cause is None:
            return None
        actions = source_results.required_actions(conn, root_id)
        target = commitment["target"]
        first_missing = read.action_plan(conn, root, actions, target)
        if first_missing is None or any(not authority.required_effect(root, action, target) for action in actions):
            return None
        matches = (
            source_delivery_pause.accepted_worker_delivery_matches(conn, config, root, round_, actions, target)
            if root["worker_result_id"] is not None
            else _model_comment_matches(conn, config, root, round_, actions)
        )
        if not matches:
            return None
        pause = source_delivery_pauses.latest(conn, root_id)
        owner = source_delivery_recovery.for_generation(conn, root_id, pause["generation"]) if pause else None
        if (
            owner is not None
            and (owner["completed_at"] is not None or source_delivery_recovery.applied_bound(conn, owner) is None)
        ) or (pause is not None and owner is None and source_delivery_pauses.dormant_actions(conn, pause) is None):
            return None
        active = round_["status"] == SourceRoundStatus.ACTIVE
        proof = {
            "version": 1,
            "revision": revision,
            "cause": cause,
            "commitment": commitment,
            "round_finished_at": now if active else round_["finished_at"],
            "pause_sha256": context_digest(dict(pause)) if pause is not None else None,
            "transition_id": owner["transition_id"] if owner else None,
            "first_missing": first_missing,
            "action_sha256": read.action_digests(actions),
        }
    except (AttributeError, IndexError, KeyError, TypeError, ValueError, RecursionError):
        return None

    checkpoint = "retire_accepted_source_delivery"
    transactions.savepoint(conn, checkpoint)
    try:
        changed = (
            write.retire(conn, root, actions[first_missing:], proof=proof, revision=revision, now=now)
            and (not active or write.close_canceled_round(conn, root, now=now))
            and (owner is None or recovery.suspend(conn, owner, now=now))
            and read.is_retired(conn, root_id)
        )
        if changed:
            if active:
                source_request_authority.revoke_unhandled_disposition(conn, ticket, checked, now=now)
            _projection(conn, root, round_, now)
            if pause is not None:
                source_attention.resolve_delivery_recovery(
                    conn, ticket.source_id, root_id, pause["generation"], now=now
                )
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.finish_isolation(conn, checkpoint, rollback=not changed)
    return root_id if changed else None
