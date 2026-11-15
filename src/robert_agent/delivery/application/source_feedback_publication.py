"""Publish one closed required GitHub feedback effect or reconcile its original intent."""

import os
from collections.abc import Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass

from robert_agent.adapters.github.source_feedback import FeedbackTransport
from robert_agent.adapters.tapd.client import TapdClient, TapdError
from robert_agent.delivery import tapd_policy
from robert_agent.delivery.application import source_delivery_observation, source_publication_queue
from robert_agent.delivery.application import source_results as authority
from robert_agent.delivery.lock import source_publication_lock
from robert_agent.foundation import values
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.foundation.contracts.delivery import PublicationStatus, PublishStatus
from robert_agent.persistence import database, execution_control, transactions
from robert_agent.persistence.queries import source_feedback as read
from robert_agent.persistence.queries import source_publication as schedule
from robert_agent.persistence.queries import source_results, source_sessions
from robert_agent.persistence.rounds.source_evidence import context_digest
from robert_agent.persistence.stores import source_feedback as write
from robert_agent.persistence.stores import source_sessions as fence
from robert_agent.work import source_attention, source_pr_selection
from robert_agent.work.application import source_github_observation
from robert_agent.work.application.source_observation import read_ticket_snapshot
from robert_agent.work.tapd_authority import TicketIdentity

_STARTED = (PublicationStatus.EXECUTING, PublicationStatus.ATTENTION)


@dataclass(frozen=True, slots=True)
class FeedbackOutcome:
    status: str
    reason: str
    receipt: read.FeedbackReceipt | None = None


@contextmanager
def _writer(db_path):
    with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
        transactions.begin_immediate(conn)
        yield conn


def _current_packet(conn, config, ticket, row):
    action = read.planned_action(row)
    if action is None:
        return None
    target = action.target
    repo = source_github_observation.configured_repository(conn, config, ticket, target["repo_id"])
    if repo is None or repo["full_name"] != target["repository"]:
        return None
    publisher = repo.get("github_account")
    if not isinstance(publisher, str):
        return None
    packet = read.packet_for(conn, row, publisher.casefold())
    if packet is None or not _closed_body(row, packet):
        return None
    return packet


def _closed_body(row, packet):
    if packet["recipe"]["mode"] == "status_only":
        return True
    projection = read.code_projection(row)
    try:
        changes = [tapd_policy.CodeChange(path, operation) for operation, path in projection["changed_paths"]]
        checks = [tapd_policy.VerificationResult(*item) for item in projection["checks"]]
        rendered = tapd_policy.project_github_output(
            changes=changes,
            verification=checks,
            approved_paths=tuple(item.path for item in changes),
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        return False
    return bool(changes) and packet["body"] == rendered.body + "\n\n<!-- " + packet["recipe"]["marker"] + " -->"


def _prefix(conn, ticket, row):
    target = {
        "provider": "tapd",
        "host": ticket.host,
        "workspace_id": ticket.workspace_id,
        "kind": ticket.kind,
        "external_id": ticket.external_id,
    }
    return (
        source_results.confirmed_required_effects(
            conn,
            row["source_result_id"],
            target,
            before_position=row["position"],
        )
        is not None
    )


def _start(db_path, config, ticket, claim, packet, snapshot, mixed, boundary, transport):
    root_id, action_id = packet["root_id"], packet["action_id"]
    with _writer(db_path) as conn:
        now = values.utc_now()
        grant = authority.authorize_delivery(
            conn,
            config,
            ticket,
            claim,
            source_result_id=root_id,
            action_id=action_id,
            observation=snapshot,
            now=now,
            mixed_observation=mixed,
            feedback_effect=boundary,
        )
        row = read.required_action(conn, root_id, action_id)
        if (
            grant is None
            or row is None
            or transport.is_set()
            or not source_delivery_observation.is_current(mixed)
            or _current_packet(conn, config, ticket, row) != packet
            or not _prefix(conn, ticket, row)
            or source_sessions.blockers_for_publication(conn, ticket.source_id, root_id)
            or source_results.unknown_github_effect(conn, ticket.source_id, require_positive_receipt=True)
            or (row["publication_id"] is not None and read.prepared_request(conn, row) != packet)
        ):
            return None
        intent = execution_control.prepare_publication_intent(
            conn,
            action_id,
            f"native-feedback:{action_id}",
            boundary.request_fingerprint,
            now,
        )
        publication_id = intent["publication_id"]
        if row["publication_id"] is None:
            write.insert_preparation(conn, read.required_action(conn, root_id, action_id), publication_id, packet)
        execution_control.transition_publication_intent(conn, publication_id, PublicationStatus.EXECUTING, now)
        return publication_id


def _parent(transport, lineage, packet):
    if lineage is None:
        return None
    target = packet["target"]
    parent = transport.discussion.parent(target["repository"], target["pr_number"])
    return parent if source_pr_selection.current_observation(lineage, parent, lineage.author) is not None else None


def _readback(db_path, claim, packet, lineage, publication_id, transport):
    root_id, action_id = packet["root_id"], packet["action_id"]
    parent = _parent(transport, lineage, packet)
    proof = transport.readback(packet, repository_id=parent["base"]["repo"].get("id")) if parent is not None else None
    observed_at = values.utc_now()
    with _writer(db_path) as conn:
        now = values.utc_now()
        row = read.required_action(conn, root_id, action_id)
        if (
            not fence.lock_claim(conn, claim, now=now)
            or row is None
            or row["source_id"] != claim.source_id
            or row["publication_id"] != publication_id
            or row["intent_status"] not in _STARTED
            or read.prepared_request(conn, row) != packet
        ):
            return FeedbackOutcome("unknown", "source_fence_or_scope_lost")
        receipt = None if transport.is_set() else write.confirm(conn, row, proof, observed_at, now)
        if receipt:
            source_attention.confirm_publication(
                conn,
                claim,
                root_id=root_id,
                action_id=action_id,
                provider="github",
                publication_id=publication_id,
                now=now,
            )
            return FeedbackOutcome("published", "remote_effect_confirmed", receipt)
        reason = "native_github_effect_unconfirmed"
        execution_control.transition_publication_intent(
            conn,
            publication_id,
            PublicationStatus.ATTENTION,
            now,
            error={"reason": reason},
        )
        source_attention.observe_publication_unknown(
            conn,
            claim,
            root_id=root_id,
            action_id=action_id,
            provider="github",
            publication_id=publication_id,
            reason=reason,
            now=now,
        )
        return FeedbackOutcome("unknown", reason)


def _execute(
    db_path,
    config,
    ticket,
    project,
    claim,
    row,
    packet,
    lineage,
    *,
    transport,
    observation_provider,
    tapd_client_factory,
    github_discussion_runner,
):
    if row["intent_status"] in _STARTED:
        if packet is None:
            return FeedbackOutcome("unknown", "saved_native_request_unavailable")
        return _readback(db_path, claim, packet, lineage, row["publication_id"], transport)
    if packet is None or not callable(observation_provider):
        return FeedbackOutcome("deferred", "native_proof_unavailable")
    boundary = source_github_observation.FeedbackPrestart(context_digest(packet))
    try:
        settings = config["tapd"]
        client = tapd_client_factory(executable=settings["executable"], cwd=settings["cwd"], environment=os.environ)
        snapshot = read_ticket_snapshot(client, ticket, project, observation_provider, stop=transport)
        mixed = source_delivery_observation.observe(
            db_path,
            config,
            ticket,
            claim,
            root_id=packet["root_id"],
            action_id=packet["action_id"],
            snapshot=snapshot,
            runner=github_discussion_runner,
            stop=transport,
            feedback_effect=boundary,
        )
    except ControlDeadlineExceeded:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, TapdError):
        return FeedbackOutcome("deferred", "native_preflight_unavailable")
    if mixed is None:
        return FeedbackOutcome("deferred", "current_source_grant_unavailable")
    if not transport.publisher_matches(packet["publisher"]) or _parent(transport, lineage, packet) is None:
        return FeedbackOutcome("deferred", "github_feedback_target_unavailable")
    started = _start(db_path, config, ticket, claim, packet, snapshot, mixed, boundary, transport)
    if started is None:
        return FeedbackOutcome("deferred", "current_source_grant_unavailable")
    if not transport.is_set():
        transport.post(packet)
    return _readback(db_path, claim, packet, lineage, started, transport)


def publish_feedback_action(
    db_path,
    config,
    *,
    source_result_id,
    action_id,
    owner,
    observation_provider=None,
    tapd_client_factory=TapdClient,
    runner=None,
    github_discussion_runner=None,
    stop=None,
    respect_backoff=False,
    reconcile_only=False,
):
    """Run the exact controller-owned feedback effect, using saved scope for readback only."""
    tapd = config.get("tapd") if isinstance(config, Mapping) else None
    if not isinstance(tapd, Mapping) or tapd.get("enabled") is not True:
        return FeedbackOutcome("deferred", "tapd_disabled")
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 512:
        return FeedbackOutcome("invalid", "source_owner_required")
    if stop is not None and stop.is_set():
        return FeedbackOutcome("deferred", "stopped")
    with closing(database.connect(db_path)) as conn:
        row = read.required_action(conn, source_result_id, action_id)
        source = source_results.tapd_source(conn, row["source_id"]) if row is not None else None
    if source is None:
        return FeedbackOutcome("invalid", "accepted_feedback_action_unavailable")
    try:
        ticket = TicketIdentity(source["host"], source["project_key"], source["source_type"], source["external_id"])
    except (TypeError, ValueError):
        return FeedbackOutcome("invalid", "native_source_scope_unavailable")
    if ticket.source_id != source["source_id"] or tapd.get("host") != ticket.host:
        return FeedbackOutcome("invalid", "native_source_scope_unavailable")
    with source_publication_lock(db_path, ticket.source_id) as locked:
        if not locked:
            return FeedbackOutcome("deferred", "source_publication_locked")
        with _writer(db_path) as conn:
            now = values.utc_now()
            row = read.required_action(conn, source_result_id, action_id)
            if row is None or row["source_id"] != ticket.source_id:
                return FeedbackOutcome("invalid", "native_action_scope_changed")
            receipt = read.confirmed_receipt(conn, source_result_id, action_id)
            if receipt is not None:
                return FeedbackOutcome("deduplicated", "remote_effect_already_confirmed", receipt)
            if row["publish_status"] != PublishStatus.NOT_PUBLISHED or row["intent_status"] not in (
                None,
                PublicationStatus.PREPARED,
                *_STARTED,
            ):
                return FeedbackOutcome("deferred", "native_effect_not_retryable")
            if respect_backoff and not schedule.action_is_due(conn, action_id, now):
                return FeedbackOutcome("deferred", "publication_backoff")
            started = row["intent_status"] in _STARTED
            project = None
            if not started:
                matching = [
                    item
                    for item in tapd.get("projects", ())
                    if isinstance(item, Mapping) and item.get("workspace_id") == ticket.workspace_id
                ]
                if len(matching) != 1:
                    return FeedbackOutcome("deferred", "native_source_scope_unavailable")
                project = matching[0]
            if not started and (reconcile_only or not _prefix(conn, ticket, row)):
                return FeedbackOutcome(
                    "deferred", "reconciliation_only" if reconcile_only else "required_predecessor_unconfirmed"
                )
            claim = fence.acquire_for_owner(conn, source_id=ticket.source_id, owner=owner, now=now, lease_seconds=300)
            if claim is None:
                return FeedbackOutcome("deferred", "source_fence_unavailable")
            packet = read.prepared_request(conn, row) if started else _current_packet(conn, config, ticket, row)
            planned = read.planned_action(row)
            lineage = read.lineage(conn, row, planned) if packet is not None and planned else None
        outcome = _execute(
            db_path,
            config,
            ticket,
            project,
            claim,
            row,
            packet,
            lineage,
            transport=FeedbackTransport(runner=runner, stop=stop),
            observation_provider=observation_provider,
            tapd_client_factory=tapd_client_factory,
            github_discussion_runner=github_discussion_runner,
        )
        if respect_backoff:
            source_publication_queue.pace_action(
                db_path,
                claim,
                source_result_id,
                action_id,
                outcome,
                provider="github",
                stop=stop,
            )
        return outcome
