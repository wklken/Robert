"""Hold saved delivery while releasing the canceled round's settled execution."""

import json

from robert_agent.delivery.application import native_preparation
from robert_agent.delivery.application import source_results as authority
from robert_agent.delivery.native_contract import selectors_for
from robert_agent.delivery.tapd_policy import prepare_comment
from robert_agent.foundation.contracts.delivery import OutputType, SourceResultKind
from robert_agent.foundation.contracts.work import SourceRoundStatus
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import transactions
from robert_agent.persistence.queries import (
    result_contract,
    source_delivery_recovery,
    source_members,
    source_results,
    source_sessions,
)
from robert_agent.persistence.queries import source_delivery_pauses as read
from robert_agent.persistence.queries import (
    tapd_intake as intake_queries,
)
from robert_agent.persistence.rounds.source_evidence import json_object
from robert_agent.persistence.stores import source_delivery_pauses as write
from robert_agent.persistence.stores import source_delivery_recovery as recoveries
from robert_agent.persistence.stores import source_sessions as fence
from robert_agent.work.application import source_request_authority, tapd_intake


def accepted_worker_delivery_matches(conn, config, root, round_, actions, target):
    result_id = root["worker_result_id"]
    candidate = source_results.worker_candidate(conn, result_id) if result_id else None
    binding = result_contract.task_round_binding(conn, candidate["task_id"]) if candidate is not None else None
    if binding is None:
        return False
    evidence = json_object(root["evidence_json"]) or {}
    sets = source_results.required_action_sets(conn, root, actions)
    if sets is None:
        return False
    actions, feedback = sets
    if any(authority.accepted_delivery_lineage(conn, config, root, round_, row)[0] is None for row in feedback):
        return False
    output = candidate["output_type"]
    metadata = json_object(candidate["metadata_json"]) or {}
    audit = metadata.get("audit")
    consumed = json.loads(candidate["consumed_event_fingerprints_json"])
    handled = [event["fingerprint"] for member in evidence["handled_members"] for event in member["events"]]
    if (
        not isinstance(audit, dict)
        or not isinstance(consumed, list)
        or len(consumed) != len(handled)
        or set(consumed) != set(handled)
        or root["kind"]
        != (SourceResultKind.QUESTION if output == OutputType.WAITING_FOR_USER else SourceResultKind.ANSWER)
        or audit.get("ok") is not True
        or json_object(root["payload_json"]) != {"output_type": output}
        or audit.get("planned_actions") != selectors_for(output)
        or len(actions) != len(audit["planned_actions"])
    ):
        return False
    for index, (action, selector) in enumerate(zip(actions, audit["planned_actions"], strict=True)):
        action_metadata = json_object(action["metadata_json"]) or {}
        repos, _repo_id = authority.accepted_delivery_lineage(conn, config, root, round_, action)
        expected_target = target if action["provider"] == "tapd" else authority.github_candidate_target(binding, output)
        if (
            repos is None
            or json_object(action["target_json"]) != expected_target
            or selector
            != {
                "provider": action["provider"],
                "type": action["action_type"],
                "target_id": action_metadata.get("target_id"),
            }
        ):
            return False
        if action["provider"] == "tapd":
            if native_preparation.worker_tapd_body(dict(action), metadata) is None or (
                action["prepared_evidence_json"] is not None
                and not _tapd_prepared_matches(conn, root, action, metadata, output, actions[:index])
            ):
                return False
        elif native_preparation.github_effect_metadata(dict(action), audit.get("action_scope")) is None:
            return False
    return True


def _tapd_prepared_matches(conn, root, action, metadata, output, preceding):
    saved = json_object(action["prepared_evidence_json"])
    if saved is None or utc_timestamp(saved.get("publisher_observed_at")) is None:
        return False
    body = native_preparation.delivered_worker_tapd_body(
        conn, root["source_result_id"], action, metadata, output, preceding
    )
    if body is None or any(
        not isinstance(saved.get(key), str) or not saved[key].strip()
        for key in ("publisher_principal", "publisher_identity_evidence_id")
    ):
        return False
    packet = prepare_comment(body, saved["receipt_id"], bot_name=saved["bot_name"], reply_name=saved["reply_name"])
    return all(
        saved.get(key) == getattr(packet, key)
        for key in ("body", "body_digest", "normalization_version", "receipt_id", "bot_name", "reply_name")
    )


def pause_delivery(conn, config, ticket, claim, *, round_id, revision, now, disposition=None):
    """Save the exact fresh dormant proof and close the round in one writer savepoint."""
    if not revision or not conn.in_transaction or claim.source_id != ticket.source_id:
        return None
    round_ = fence.lock_round(conn, claim, round_id, now=now)
    root = source_results.for_round(conn, round_id) if round_ is not None else None
    if root is None or root["source_id"] != ticket.source_id or read.latest(conn, root["source_result_id"]):
        return None
    cancellation = intake_queries.round_cancellation(conn, round_id)
    try:
        if source_members.round_github_request_ids(conn, round_id):
            checked = source_request_authority.current_disposition(
                conn,
                config,
                ticket,
                claim,
                root["source_result_id"],
                disposition,
                revision=revision,
                now=now,
            )
            scope = tapd_intake.paused_mixed_delivery_scope(
                conn, config, ticket, root, round_, checked, revision=revision
            )
        else:
            members = source_results.consumed_members(conn, root, round_)
            scope = tapd_intake.paused_delivery_scope(conn, config, ticket, members, revision=revision)
        if scope is None:
            return None
        proof = read.materialized_proof(conn, root, round_, cancellation, scope, revision=revision)
        actions = source_results.required_actions(conn, root["source_result_id"])
        if proof is None or not accepted_worker_delivery_matches(conn, config, root, round_, actions, proof["target"]):
            return None
    except (IndexError, KeyError, TypeError, ValueError, RecursionError):
        return None

    checkpoint = "pause_source_delivery"
    transactions.savepoint(conn, checkpoint)
    try:
        changed = write.record(conn, root=root, revision=revision, proof=proof, now=now) and fence.finish_round(
            conn, claim, round_id=round_id, outcome=SourceRoundStatus.CANCELED, now=now
        )
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.finish_isolation(conn, checkpoint, rollback=not changed)
    return root["source_result_id"] if changed else None


def pause_recovery(conn, config, ticket, claim, owner, *, revision, now):
    """Re-dormant only a settled missing suffix after another proved same-rule loss."""
    if (
        not revision
        or not conn.in_transaction
        or claim.source_id != ticket.source_id
        or not fence.lock_claim(conn, claim, now=now)
        or source_delivery_recovery.active(conn, ticket.source_id) != owner
        or source_delivery_recovery.applied_bound(conn, owner) is None
        or source_sessions.active_round(conn, ticket.source_id) is not None
        or source_sessions.blockers_for_publication(conn, ticket.source_id, owner["source_result_id"])
    ):
        return None
    root = source_results.for_source(conn, ticket.source_id, owner["source_result_id"])
    round_ = source_results.delivery_round(conn, ticket.source_id, root["round_id"]) if root else None
    if round_ is None or round_["status"] != SourceRoundStatus.CANCELED:
        return None
    try:
        if source_members.round_github_request_ids(conn, root["round_id"]):
            return None
        members = source_results.consumed_members(conn, root, round_)
        scope = tapd_intake.paused_delivery_scope(conn, config, ticket, members, revision=revision)
        if scope is None:
            return None
        proof = read.materialized_proof(
            conn,
            root,
            round_,
            intake_queries.round_cancellation(conn, root["round_id"]),
            scope,
            revision=revision,
            execution_finished_at=round_["finished_at"],
        )
        actions = source_results.required_actions(conn, root["source_result_id"])
        if (
            proof is None
            or not any(item["disposition"] == "local_never_started" for item in proof["effects"])
            or not accepted_worker_delivery_matches(conn, config, root, round_, actions, proof["target"])
        ):
            return None
    except (IndexError, KeyError, TypeError, ValueError, RecursionError):
        return None
    checkpoint = "pause_recovering_source_delivery"
    transactions.savepoint(conn, checkpoint)
    try:
        changed = recoveries.suspend(conn, owner, now=now) and write.record(
            conn, root=root, revision=revision, proof=proof, now=now
        )
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.finish_isolation(conn, checkpoint, rollback=not changed)
    return root["source_result_id"] if changed else None
