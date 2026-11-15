"""Publish or reconcile one accepted native TAPD comment."""

import os
from collections.abc import Mapping
from contextlib import closing, contextmanager, nullcontext
from dataclasses import asdict, dataclass
from uuid import uuid4

from robert_agent.adapters.tapd.client import TapdClient, TapdError
from robert_agent.adapters.tapd.publication import TapdPublisherIdentity
from robert_agent.delivery.application import (
    native_preparation,
    source_completion,
    source_delivery_observation,
    source_delivery_resume,
    source_pr_lineage,
    source_publication_queue,
)
from robert_agent.delivery.application import (
    source_results as authority,
)
from robert_agent.delivery.lock import source_publication_lock, task_publication_lock
from robert_agent.delivery.tapd_policy import ObservedReceipt, prepare_comment, reconcile_comment
from robert_agent.foundation import values
from robert_agent.foundation.contracts.delivery import AuditStatus, PublicationStatus, PublishStatus
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import database, errors, execution_control, transactions
from robert_agent.persistence.queries import (
    source_confirmation,
    source_delivery_recovery,
    source_members,
)
from robert_agent.persistence.queries import (
    source_publication as schedule,
)
from robert_agent.persistence.queries import (
    source_results as read,
)
from robert_agent.persistence.queries import (
    source_sessions as sessions,
)
from robert_agent.persistence.stores import (
    source_observations,
    source_results,
)
from robert_agent.persistence.stores import (
    source_publication as write,
)
from robert_agent.persistence.stores import (
    source_sessions as fence,
)
from robert_agent.work import source_attention
from robert_agent.work import tapd_authority as facts
from robert_agent.work.application import source_github_observation, source_observation, tapd_intake
from robert_agent.work.application.source_observation import ScanLimits, read_ticket_snapshot

_STARTED = (PublicationStatus.EXECUTING, PublicationStatus.ATTENTION)
_LIMITS = ScanLimits()
_LEASE_SECONDS = 2 * _LIMITS.seconds + 60
_REMOTE_FAILURES = (TapdError, OSError, ValueError, TypeError)


@dataclass(frozen=True, slots=True)
class TapdPublicationOutcome:
    status: str
    reason: str
    external_id: str | None = None
    completion: source_completion.CompletionOutcome | None = None


@contextmanager
def _writer(db_path):
    with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
        transactions.begin_immediate(conn)
        yield conn


def _root_action(conn, root_id, action_id):
    root = read.by_id(conn, root_id)
    source = read.tapd_source(conn, root["source_id"]) if root else None
    if source is None:
        return None
    try:
        ticket = facts.TicketIdentity(
            source["host"], source["project_key"], source["source_type"], source["external_id"]
        )
    except (TypeError, ValueError):
        return None
    if ticket.source_id != source["source_id"]:
        return None
    target = read.source_target(source)
    actions = read.required_actions(conn, root_id)
    action = next((row for row in actions if row["action_id"] == action_id), None)
    if (
        action is None
        or action["provider"] != "tapd"
        or action["action_type"] != "comment"
        or action["audit_status"] != AuditStatus.ACCEPTED
        or values.json_object(action["target_json"]) != target
    ):
        return None
    if root["worker_result_id"] is not None:
        if (
            action["source_result_id"] is not None
            or action["result_id"] != root["worker_result_id"]
            or action["task_id"] != values.json_object(root["evidence_json"]).get("task_id")
        ):
            return None
    elif action["source_result_id"] != root_id or action["task_id"] is not None:
        return None
    return ticket, target, root, action, actions


def _claim(conn, ticket, owner, claim, now):
    if claim is None:
        return fence.acquire_for_owner(
            conn, source_id=ticket.source_id, owner=owner, now=now, lease_seconds=_LEASE_SECONDS
        )
    if claim.source_id == ticket.source_id and (
        fence.renew(conn, claim, now=now, lease_seconds=_LEASE_SECONDS) or fence.lock_claim(conn, claim, now=now)
    ):
        return claim
    return None


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 512


def _publisher_identity(provider, client, ticket, project, stop):
    started = utc_timestamp(values.utc_now())
    proof = provider(client, ticket, project=project, stop=stop)
    ended = utc_timestamp(values.utc_now())
    if not isinstance(proof, TapdPublisherIdentity):
        return None
    seen = utc_timestamp(proof.observed_at)
    if (
        (proof.host, proof.workspace_id) != (ticket.host, ticket.workspace_id)
        or not _nonempty(proof.principal)
        or not _nonempty(proof.evidence_id)
        or seen is None
        or not started <= seen <= ended
    ):
        return None
    return proof


def _candidate_body(conn, root, action, preceding):
    if root["worker_result_id"] is None:
        return values.json_object(action["metadata_json"]).get("body")
    candidate = read.worker_candidate(conn, root["worker_result_id"])
    if candidate is None:
        return None
    return native_preparation.delivered_worker_tapd_body(
        conn, root["source_result_id"], action, candidate["metadata_json"], candidate["output_type"], preceding
    )


def _prepared_evidence(body, saved, *, tapd, action_id, target, identity):
    prepared = prepare_comment(
        body,
        saved.get("receipt_id") if saved else f"rbt-{uuid4().hex}",
        bot_name=tapd["bot_name"],
        reply_name=tapd["reply_name"],
    )
    evidence = {
        **asdict(prepared),
        "action_id": action_id,
        "target": target,
        "publisher_principal": identity.principal,
        "publisher_identity_evidence_id": identity.evidence_id,
        "publisher_observed_at": identity.observed_at,
    }
    if not saved:
        return evidence
    if (
        not _nonempty(saved.get("publisher_identity_evidence_id"))
        or utc_timestamp(saved.get("publisher_observed_at")) is None
    ):
        return None
    evidence.update(
        publisher_identity_evidence_id=saved["publisher_identity_evidence_id"],
        publisher_observed_at=saved["publisher_observed_at"],
    )
    return evidence if evidence == saved else None


def _start(db_path, config, ticket, claim, root_id, action_id, task_id, snapshot, identity, stop, recovery, mixed=None):
    with _writer(db_path) as conn:
        selected = _root_action(conn, root_id, action_id)
        if selected is None or selected[0] != ticket or selected[3]["task_id"] != task_id:
            return TapdPublicationOutcome("invalid", "action_scope_changed")
        _, target, root, action, _ = selected
        now = values.utc_now()
        if not authority.authorize_delivery(
            conn,
            config,
            ticket,
            claim,
            source_result_id=root_id,
            action_id=action_id,
            observation=snapshot,
            now=now,
            recovery_observation=recovery,
            mixed_observation=mixed,
        ):
            return TapdPublicationOutcome("deferred", "source_grant_unavailable")
        preceding = read.confirmed_required_effects(conn, root_id, target, before_position=action["position"])
        if preceding is None:
            return TapdPublicationOutcome("deferred", "predecessor_receipt_unavailable")
        if sessions.blockers_for_publication(conn, ticket.source_id, root_id) or read.unknown_github_effect(
            conn, ticket.source_id, require_positive_receipt=True
        ):
            return TapdPublicationOutcome("deferred", "source_busy")
        if not source_pr_lineage.ensure_root_pr_lineage(conn, claim, source_result_id=root_id, now=now):
            return TapdPublicationOutcome("deferred", "source_pr_lineage_unavailable")
        if stop is not None and stop.is_set():
            return TapdPublicationOutcome("deferred", "stopped")
        if mixed is not None and not source_delivery_observation.is_current(mixed):
            return TapdPublicationOutcome("deferred", "source_grant_unavailable")
        body = _candidate_body(conn, root, action, preceding)
        if not isinstance(body, str) or not body or action["external_id"] is not None:
            return TapdPublicationOutcome("invalid", "candidate_body_unavailable")
        saved = values.json_object(action["prepared_evidence_json"])
        try:
            evidence = _prepared_evidence(
                body, saved, tapd=config["tapd"], action_id=action_id, target=target, identity=identity
            )
        except (KeyError, ValueError):
            evidence = None
        if evidence is None or (action["publication_id"] is not None and not saved):
            return TapdPublicationOutcome("invalid", "prepared_comment_unavailable")
        if source_delivery_resume.is_recovered_tapd_observation(
            recovery
        ) and not source_delivery_resume.recovered_tapd_is_current(recovery):
            return TapdPublicationOutcome("deferred", "source_grant_unavailable")
        fingerprint = facts.digest(evidence)
        if action["publication_id"] is not None and action["intent_fingerprint"] != fingerprint:
            return TapdPublicationOutcome("invalid", "prepared_comment_mismatch")
        intent = execution_control.prepare_publication_intent(
            conn, action_id, f"tapd-comment:{action_id}", fingerprint, now
        )
        if not saved:
            source_results.insert_tapd_publication_preparation(
                conn, publication_id=intent["publication_id"], evidence=evidence
            )
        execution_control.transition_publication_intent(
            conn, intent["publication_id"], PublicationStatus.EXECUTING, now
        )
        scope = _verified_scope(conn, config, ticket, claim, root_id, action_id, now)
        if scope is None:
            raise ValueError("Started TAPD publication lacks its prepared proof")
        return scope


def _verified_scope(conn, config, ticket, claim, root_id, action_id, now):
    scope = authority.tapd_reconciliation_scope(
        conn, config, ticket, claim, source_result_id=root_id, action_id=action_id, now=now
    )
    selected = _root_action(conn, root_id, action_id) if scope else None
    if selected is None:
        return None
    action = selected[3]
    prepared = values.json_object(action["prepared_evidence_json"])
    if (
        not _nonempty(prepared.get("publisher_identity_evidence_id"))
        or utc_timestamp(prepared.get("publisher_observed_at")) is None
        or action["intent_fingerprint"] != facts.digest(prepared)
    ):
        return None
    return scope


def _read_receipt(snapshot, ticket, target, action_id, scope):
    entry_type = ticket.entry_types[0]
    observations, proofs = [], {}
    seen = set()
    for item in snapshot.comments:
        version = item.version
        if version.ticket != ticket or version.event_id in seen:
            return None
        seen.add(version.event_id)
        creator, own = item.creator, item.own_body
        verified = (
            version.entry_type == entry_type
            and isinstance(creator, facts.CreatorEvidence)
            and isinstance(own, facts.OwnBodyEvidence)
            and creator.observation == own.observation == version
            and creator.principal == own.principal == scope.publisher_principal
            and _nonempty(creator.evidence_id)
            and _nonempty(own.evidence_id)
        )
        if verified:
            proofs[version.external_id] = {
                "identity_evidence_id": creator.evidence_id,
                "own_body_evidence_id": own.evidence_id,
            }
        observations.append(
            ObservedReceipt(
                version.external_id,
                ticket.key,
                creator.principal if verified else None,
                item.body_html,
                version.reply_id,
            )
        )
    match = reconcile_comment(
        observations,
        prepared=scope.prepared,
        target=ticket.key,
        expected_identity=scope.publisher_principal,
        complete=snapshot.coverage == "complete",
    )
    if match.status != "matched":
        return None
    return {
        "action_id": action_id,
        "target": target,
        "entry_type": entry_type,
        "external_id": match.external_id,
        "receipt_id": scope.prepared.receipt_id,
        "body_digest": scope.prepared.body_digest,
        "publisher_principal": scope.publisher_principal,
        **proofs[match.external_id],
        "confirmed_at": values.utc_now(),
    }


def _finish_delivery(conn, config, claim, root_id, *, now, snapshot=None):
    """Preserve a confirmed receipt even when the subsequent local transition is held."""
    if snapshot is not None:
        checkpoint = "observe_source_delivery"
        transactions.savepoint(conn, checkpoint)
        try:
            ticket = snapshot.facts.identity
            observed = tapd_intake.observe_ticket(
                conn, config["tapd"], snapshot, source_observation.publication_facts(conn, ticket), now=now
            )
        except (ValueError, errors.IntegrityError):
            transactions.finish_isolation(conn, checkpoint, rollback=True)
            source = read.tapd_source(conn, claim.source_id)
            if source:
                source_observations.hold_source(
                    conn,
                    claim.source_id,
                    values.json_object(source["metadata_json"]),
                    "source_readback_unavailable",
                    now,
                )
            return source_completion.CompletionOutcome("held", "source_readback_unavailable")
        transactions.release(conn, checkpoint)
        if observed.revision is None and observed.reason != "stale_observation":
            return source_completion.CompletionOutcome("held", "source_readback_unavailable")
    if source_confirmation.for_result(conn, claim.source_id, root_id) is not None:
        return None
    try:
        return source_completion.complete_source_result(conn, config, claim, source_result_id=root_id, now=now)
    except (ValueError, errors.IntegrityError):
        # The completion use case rolls back its own mutations.
        return source_completion.CompletionOutcome("held", "source_completion_inconsistent")


def _settle(db_path, config, ticket, claim, root_id, action_id, scope, receipt, reason, *, snapshot=None):
    with _writer(db_path) as conn:
        now = values.utc_now()
        if _verified_scope(conn, config, ticket, claim, root_id, action_id, now) != scope:
            return TapdPublicationOutcome("unknown", "source_fence_or_scope_lost")
        if receipt is not None and write.confirm_tapd_comment(
            conn,
            source_id=ticket.source_id,
            source_result_id=root_id,
            action_id=action_id,
            publication_id=scope.publication_id,
            receipt=receipt,
            now=now,
        ):
            source_attention.confirm_publication(
                conn,
                claim,
                root_id=root_id,
                action_id=action_id,
                provider="tapd",
                publication_id=scope.publication_id,
                now=now,
            )
            completion = _finish_delivery(conn, config, claim, root_id, now=now, snapshot=snapshot)
            return TapdPublicationOutcome("published", "receipt_confirmed", receipt["external_id"], completion)
        execution_control.transition_publication_intent(
            conn,
            scope.publication_id,
            PublicationStatus.ATTENTION,
            now,
            error={"reason": reason},
        )
        source_attention.observe_publication_unknown(
            conn,
            claim,
            root_id=root_id,
            action_id=action_id,
            provider="tapd",
            publication_id=scope.publication_id,
            reason=reason,
            now=now,
        )
        return TapdPublicationOutcome("unknown", reason)


def _publish_locked(
    db_path,
    config,
    selected,
    project,
    owner,
    claim,
    client_factory,
    observation_provider,
    identity_provider,
    stop,
    respect_backoff,
    github_runner,
    github_discussion_runner,
):
    ticket, target, root, action, _ = selected
    root_id, action_id = root["source_result_id"], action["action_id"]
    with _writer(db_path) as conn:
        current = _root_action(conn, root_id, action_id)
        if current is None or current[0] != ticket or current[3]["task_id"] != action["task_id"]:
            return TapdPublicationOutcome("invalid", "action_scope_changed")
        action = current[3]
        if (
            respect_backoff
            and action["publish_status"] != PublishStatus.PUBLISHED
            and not schedule.action_is_due(conn, action_id, values.utc_now())
        ):
            return TapdPublicationOutcome("deferred", "publication_backoff")
        confirmed = read.confirmed_comment(action, target)
        if confirmed is not None:
            now = values.utc_now()
            held = _claim(conn, ticket, owner, claim, now)
            if held is not None:
                source_attention.confirm_publication(
                    conn,
                    held,
                    root_id=root_id,
                    action_id=action_id,
                    provider="tapd",
                    publication_id=action["publication_id"],
                    now=now,
                )
            completion = (
                _finish_delivery(conn, config, held, root_id, now=now)
                if held
                else (source_completion.CompletionOutcome("held", "source_fence_unavailable"))
            )
            return TapdPublicationOutcome("deduplicated", "receipt_already_confirmed", confirmed[1], completion)
        if (
            action["intent_status"] == PublicationStatus.PUBLISHED
            or action["publish_status"] == PublishStatus.PUBLISHED
        ):
            now = values.utc_now()
            held = _claim(conn, ticket, owner, claim, now) if action["publication_id"] is not None else None
            if held is not None:
                source_attention.observe_publication_unknown(
                    conn,
                    held,
                    root_id=root_id,
                    action_id=action_id,
                    provider="tapd",
                    publication_id=action["publication_id"],
                    reason="published_receipt_unverified",
                    now=now,
                )
            return TapdPublicationOutcome("unknown", "published_receipt_unverified")
        started = action["intent_status"] in _STARTED
        if not started and (
            action["publish_status"] != PublishStatus.NOT_PUBLISHED
            or action["intent_status"] not in (None, PublicationStatus.PREPARED)
        ):
            return TapdPublicationOutcome("deferred", "action_not_retryable")
        if observation_provider is None:
            return TapdPublicationOutcome("unknown" if started else "deferred", "authenticated_observation_unavailable")
        if not started and identity_provider is None:
            return TapdPublicationOutcome("deferred", "publisher_identity_unavailable")
        now = values.utc_now()
        claim = _claim(conn, ticket, owner, claim, now)
        if claim is None:
            return TapdPublicationOutcome("unknown" if started else "deferred", "source_fence_unavailable")
        scope = _verified_scope(conn, config, ticket, claim, root_id, action_id, now) if started else None
    if started and scope is None:
        outcome = TapdPublicationOutcome("unknown", "prepared_scope_unavailable")
    else:
        outcome = _publish_effect(
            db_path,
            config,
            ticket,
            target,
            root_id,
            action_id,
            action["task_id"],
            project,
            claim,
            scope,
            client_factory,
            observation_provider,
            identity_provider,
            stop,
            github_runner,
            github_discussion_runner,
        )
    if respect_backoff:
        source_publication_queue.pace_action(db_path, claim, root_id, action_id, outcome, provider="tapd", stop=stop)
    return outcome


def _publish_effect(
    db_path,
    config,
    ticket,
    target,
    root_id,
    action_id,
    task_id,
    project,
    claim,
    scope,
    client_factory,
    observation_provider,
    identity_provider,
    stop,
    github_runner,
    github_discussion_runner,
):
    started = scope is not None
    tapd = config["tapd"]
    try:
        client = client_factory(executable=tapd["executable"], cwd=tapd["cwd"], environment=os.environ)
    except (*_REMOTE_FAILURES, KeyError):
        return TapdPublicationOutcome("unknown" if started else "deferred", "tapd_client_unavailable")
    if not started:
        try:
            if stop is not None and stop.is_set():
                return TapdPublicationOutcome("deferred", "stopped")
            proof = _publisher_identity(identity_provider, client, ticket, project, stop)
            if proof is None:
                return TapdPublicationOutcome("deferred", "publisher_identity_unavailable")
            snapshot = read_ticket_snapshot(client, ticket, project, observation_provider, limits=_LIMITS, stop=stop)
        except _REMOTE_FAILURES:
            return TapdPublicationOutcome("deferred", "fresh_observation_unavailable")
        with closing(database.connect(db_path)) as conn:
            owner = source_delivery_recovery.active(conn, ticket.source_id)
            recovering = owner is not None and owner["source_result_id"] == root_id
            root = read.for_source(conn, ticket.source_id, root_id)
            mixed_required = (
                root and root["round_id"] and source_members.round_github_request_ids(conn, root["round_id"])
            )
        recovery = mixed = None
        if mixed_required and not recovering:
            try:
                mixed = source_delivery_observation.observe(
                    db_path,
                    config,
                    ticket,
                    claim,
                    root_id=root_id,
                    action_id=action_id,
                    snapshot=snapshot,
                    runner=github_discussion_runner,
                    stop=stop,
                )
            except (*_REMOTE_FAILURES, errors.Error):
                return TapdPublicationOutcome("deferred", "fresh_observation_unavailable")
            if mixed is None:
                return TapdPublicationOutcome("deferred", "source_grant_unavailable")
        if recovering:
            with _writer(db_path) as conn:
                now = values.utc_now()
                if not fence.lock_claim(conn, claim, now=now):
                    return TapdPublicationOutcome("deferred", "source_fence_unavailable")
                observation = tapd_intake.observe_ticket(
                    conn, config["tapd"], snapshot, source_observation.publication_facts(conn, ticket), now=now
                )
                if mixed_required:
                    check = source_delivery_resume.recovered_tapd_check(
                        conn,
                        config,
                        ticket,
                        claim,
                        root_id,
                        action_id,
                        revision=observation.revision,
                        snapshot=snapshot,
                    )
                else:
                    check = source_delivery_resume.effect_check(
                        conn,
                        claim,
                        root_id,
                        action_id,
                        revision=observation.revision,
                    )
                if check is None:
                    source_delivery_resume.record_unproven(
                        conn,
                        config,
                        ticket,
                        claim,
                        revision=observation.revision,
                        reason="source_delivery_reprocess_required",
                        now=now,
                        root_id=root_id,
                        action_id=action_id,
                    )
            if check is None:
                return TapdPublicationOutcome("deferred", "source_delivery_reprocess_required")
            options = {}
            if mixed_required:

                def observe_discussion(recovered_scope):
                    return source_github_observation.observe_recovered_tapd(
                        db_path,
                        config,
                        claim,
                        ticket,
                        recovered_scope,
                        revision=check.revision,
                        runner=github_discussion_runner,
                        stop=stop,
                    )

                options["observe_discussion"] = observe_discussion
            recovery, reason = source_delivery_resume.observe(check, runner=github_runner, stop=stop, **options)
            if recovery is None:
                source_delivery_resume.record_stale(db_path, config, ticket, check, reason)
                return TapdPublicationOutcome("deferred", reason)
            if mixed_required:
                try:
                    latest = read_ticket_snapshot(
                        client,
                        ticket,
                        project,
                        observation_provider,
                        limits=_LIMITS,
                        stop=stop,
                    )
                except _REMOTE_FAILURES:
                    return TapdPublicationOutcome("deferred", "fresh_observation_unavailable")
                if not source_delivery_resume.same_recovered_tapd_snapshot(snapshot, latest):
                    try:
                        with _writer(db_path) as conn:
                            now = values.utc_now()
                            if fence.lock_claim(conn, claim, now=now):
                                tapd_intake.observe_ticket(
                                    conn,
                                    tapd,
                                    latest,
                                    source_observation.publication_facts(conn, ticket),
                                    now=now,
                                )
                    except _REMOTE_FAILURES:
                        pass
                    return TapdPublicationOutcome("deferred", "source_grant_unavailable")
                recovery, reason = source_delivery_resume.corroborate_recovered_tapd(recovery, latest)
                if recovery is None:
                    source_delivery_resume.record_stale(db_path, config, ticket, check, reason)
                    return TapdPublicationOutcome("deferred", reason)
        try:
            scope = _start(
                db_path, config, ticket, claim, root_id, action_id, task_id, snapshot, proof, stop, recovery, mixed
            )
        except ValueError:
            return TapdPublicationOutcome("invalid", "effect_preparation_refused")
        if isinstance(scope, TapdPublicationOutcome):
            return scope
        if source_delivery_resume.is_recovered_tapd_observation(recovery) and (
            not source_delivery_resume.recovered_tapd_is_current(recovery) or (stop is not None and stop.is_set())
        ):
            return _settle(db_path, config, ticket, claim, root_id, action_id, scope, None, "write_outcome_unknown")
        try:
            client.add_comment(ticket.workspace_id, ticket.kind, ticket.external_id, scope.prepared.body, stop=stop)
        except _REMOTE_FAILURES:
            return _settle(db_path, config, ticket, claim, root_id, action_id, scope, None, "write_outcome_unknown")
    snapshot = None
    try:
        snapshot = read_ticket_snapshot(client, ticket, project, observation_provider, limits=_LIMITS, stop=stop)
        receipt = _read_receipt(snapshot, ticket, target, action_id, scope)
    except _REMOTE_FAILURES:
        receipt = None
    return _settle(
        db_path,
        config,
        ticket,
        claim,
        root_id,
        action_id,
        scope,
        receipt,
        "readback_not_confirmed",
        snapshot=snapshot,
    )


def _hold_invalid_selection(db_path, config, owner, claim, root_id, action_id, outcome):
    with closing(database.connect(db_path)) as conn:
        root = read.by_id(conn, root_id)
        source = read.tapd_source(conn, root["source_id"]) if root else None
    if not source:
        return
    tapd = config["tapd"]
    source_id = source["source_id"]
    if (
        tapd["host"] != source["host"]
        or sum(project["workspace_id"] == source["project_key"] for project in tapd["projects"]) != 1
        or (claim is not None and claim.source_id != source_id)
    ):
        return
    with source_publication_lock(db_path, source_id) as locked:
        if not locked:
            return
        with _writer(db_path) as conn:
            if _root_action(conn, root_id, action_id) is not None or not schedule.action_is_due(
                conn, action_id, values.utc_now()
            ):
                return
            now = values.utc_now()
            held = claim if claim is not None and fence.lock_claim(conn, claim, now=now) else None
            if claim is None:
                held = fence.acquire_for_owner(conn, source_id=source_id, owner=owner, now=now, lease_seconds=60)
            if held is not None:
                write.save_action_pacing(
                    conn,
                    source_id=held.source_id,
                    source_result_id=root_id,
                    action_id=action_id,
                    fields={
                        "status": "attention",
                        "reason_code": outcome.reason,
                        "retryable": False,
                        "next_retry_at": None,
                        "failed_at": now,
                    },
                )


def publish_tapd_action(
    db_path,
    config,
    *,
    source_result_id,
    action_id,
    owner=None,
    claim=None,
    client_factory=TapdClient,
    observation_provider=None,
    identity_provider=None,
    stop=None,
    respect_backoff=False,
    github_runner=None,
    github_discussion_runner=None,
):
    """Settle one accepted comment under the source claim and publication locks.

    A trusted identity provider attests the configured client's actual endpoint
    and authenticated principal. The observation provider separately attests the
    exact ticket and each readback comment's creator and own body. Absent these
    integrations the default API does not write. An already-started intent can
    only reconcile, including after mutating authority has been revoked.
    """
    if (
        not _nonempty(source_result_id)
        or not _nonempty(action_id)
        or (claim is not None and not isinstance(claim, fence.SourceClaim))
        or not _nonempty(claim.owner if claim is not None else owner)
        or (claim is not None and owner not in (None, claim.owner))
    ):
        return TapdPublicationOutcome("invalid", "publication_identity_invalid")
    tapd = config.get("tapd") if isinstance(config, Mapping) else None
    if not isinstance(tapd, Mapping) or tapd.get("enabled") is not True:
        return TapdPublicationOutcome("deferred", "tapd_disabled")
    with closing(database.connect(db_path)) as conn:
        selected = _root_action(conn, source_result_id, action_id)
    if selected is None:
        invalid = TapdPublicationOutcome("invalid", "accepted_tapd_action_unavailable")
        if respect_backoff:
            _hold_invalid_selection(db_path, config, owner, claim, source_result_id, action_id, invalid)
        return invalid
    ticket, _, _, action, _ = selected
    projects = [project for project in tapd.get("projects", ()) if project["workspace_id"] == ticket.workspace_id]
    if tapd.get("host") != ticket.host or len(projects) != 1:
        return TapdPublicationOutcome("deferred", "tapd_scope_unavailable")
    with source_publication_lock(db_path, ticket.source_id) as acquired:
        if not acquired:
            return TapdPublicationOutcome("deferred", "source_publication_locked")
        lock = task_publication_lock(db_path, action["task_id"]) if action["task_id"] else nullcontext(True)
        with lock as acquired:
            if not acquired:
                return TapdPublicationOutcome("deferred", "task_publication_locked")
            return _publish_locked(
                db_path,
                config,
                selected,
                projects[0],
                owner,
                claim,
                client_factory,
                observation_provider,
                identity_provider,
                stop,
                respect_backoff,
                github_runner,
                github_discussion_runner,
            )
