"""Apply operation-bound GitHub request decisions in the source admission writer."""

import time
from dataclasses import asdict, dataclass
from hashlib import sha256

from robert_agent.foundation.contracts.work import (
    RequestEventState,
    SourceCapability,
)
from robert_agent.foundation.contracts.work import (
    SourceIngressPhase as Phase,
)
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import records
from robert_agent.persistence.queries import source_members, source_results, source_retirement, source_sessions
from robert_agent.persistence.rounds.source_evidence import context_digest, json_object
from robert_agent.persistence.stores import source_github_ingress as write
from robert_agent.work import tapd_authority as authority
from robert_agent.work.application import source_github_observation as observations


@dataclass(frozen=True, slots=True)
class CurrentGitHubGrant:
    request: records.Record
    decision: authority.AuthorityDecision


def _first_request_observed(first, checked):
    if first.get("mention_authorized") is True:
        return True
    # An initially unresolved observation can support the same original
    # request only when the fresh read proves its exact first body and actor.
    timestamp = utc_timestamp(first.get("body_updated_at"))
    return (
        not first.get("predates_bridge")
        and first.get("body_observed") is True
        and first.get("addressed_to_bot") is True
        and first.get("actor_trusted_or_unresolved") is True
        and timestamp is not None
        and timestamp == utc_timestamp(checked.item.updated_at)
        and first.get("body") == checked.item.body
        and first.get("actor_login") in (None, checked.item.actor)
    )


def development_parent_paused(authorization, label, binding):
    """Known TAPD rule loss is independent of GitHub discussion availability."""
    return authorization.get("capability") == SourceCapability.DEVELOPMENT and (
        label.reason in {"no_label_match", "repository_conflict"}
        or (label.reason == "allowed" and label.rule != authorization.get("rule"))
        or (binding and binding.get("state") == "paused" and binding.get("reason") != "resume_confirmation_unavailable")
    )


def pause_active_parent_requests(conn, round_, label, binding, *, now):
    """Pause exact unconsumed GitHub grants from the current TAPD observation writer."""
    if (
        not conn.in_transaction
        or round_["capability"] != SourceCapability.DEVELOPMENT
        or source_sessions.active_round(conn, round_["source_id"]) != round_
        or sha256(round_["input_snapshot_json"].encode()).hexdigest() != round_["input_sha256"]
    ):
        return ()
    frozen = (json_object(round_["input_snapshot_json"]) or {}).get("authority")
    if not isinstance(frozen, dict):
        return ()
    paused = []
    for member in source_members.frozen_members(conn, round_["round_id"]):
        owned = member.ownership
        current = json_object(owned.request["authorization_json"])
        if (
            owned.return_target is None
            or current is None
            or current != frozen.get(owned.request["request_id"])
            or owned.request["phase"] not in (Phase.PENDING, Phase.HELD, Phase.PAUSED)
            or any(event["state"] != RequestEventState.PENDING for event in owned.events)
            or not development_parent_paused(current, label, binding)
        ):
            continue
        if not write.replace_current_grant(conn, owned=owned, phase=Phase.PAUSED, now=now):
            raise ValueError("Active GitHub request changed during its parent authorization update")
        paused.append(owned.request["request_id"])
    return tuple(paused)


def _phase_and_grant(owned, checked, facts, project, binding, repository):
    current = json_object(owned.request["authorization_json"]) or {}
    first = json_object(owned.bridge["first_observation_json"]) or {}
    target = owned.return_target
    if checked.status == "absent" or checked.reason in {
        "github_current_actor_untrusted",
        "github_current_mention_removed",
    }:
        return Phase.REVOKED, None, checked.reason
    state = authority.ticket_state(facts)
    if state == "terminal":
        return Phase.REVOKED, None, "source_terminal"
    label = authority.label_authority(facts, project)
    if development_parent_paused(current, label, binding):
        return Phase.PAUSED, None, "github_development_binding_paused"
    if (
        state != "open"
        or not owned.original_item_proven
        or not _first_request_observed(first, checked)
        or not repository
        or current.get("bot_login") != repository["github_account"]
        or target["repository"] not in project["repositories"]
        or checked.status != "present"
        or checked.reason != "github_current_mention"
        or checked.semantic_version is None
        or not isinstance(checked.parent, dict)
    ):
        return Phase.HELD, None, "github_current_grant_unavailable"

    first_entry = first.get("entry_grant")
    entry = current.get("entry_grant")
    if not isinstance(first_entry, dict) or entry != first_entry:
        entry = {"capability": SourceCapability.READONLY.value, "rule": None}
    rule = entry.get("rule")
    granted = current.get("capability")
    if granted not in (None, SourceCapability.READONLY, SourceCapability.DEVELOPMENT):
        return Phase.HELD, None, "github_current_grant_unavailable"
    if granted == SourceCapability.DEVELOPMENT and (
        entry.get("capability") != SourceCapability.DEVELOPMENT or current.get("rule") != rule
    ):
        return Phase.HELD, None, "github_current_grant_unavailable"
    if label.reason in {"repository_conflict", "label_facts_unknown"}:
        return Phase.HELD, None, label.reason
    capability = SourceCapability.READONLY
    if granted != SourceCapability.READONLY and entry.get("capability") == SourceCapability.DEVELOPMENT:
        if not (
            isinstance(rule, dict)
            and rule.get("repo") == target["repository"]
            and binding
            and binding.get("state") == "active"
            and binding.get("rule") == label.rule == rule
            and label.reason == "allowed"
        ):
            return Phase.PAUSED, None, "github_development_binding_paused"
        if checked.parent.get("state") == "open" and (
            checked.parent.get("base_branch") == checked.parent.get("original_base_branch")
        ):
            capability = SourceCapability.DEVELOPMENT
        elif granted == SourceCapability.DEVELOPMENT:
            return Phase.PAUSED, None, "github_parent_development_unavailable"
    return (
        Phase.PENDING,
        authority.AuthorityDecision(
            "allowed",
            capability.value,
            (target["repository"],),
            rule if capability == SourceCapability.DEVELOPMENT else None,
        ),
        "github_current_grant",
    )


def _snapshot(owned, checked):
    target = owned.return_target
    item = checked.item
    return {
        "provider": "github",
        "event_id": target["event_id"],
        "event_fingerprint": target["event_fingerprint"],
        "item": {
            **owned.original_item,
            "updated_at": item.updated_at,
        },
        "body": item.body,
        "actor_login": item.actor.casefold(),
        "body_edited_at": item.edited_at,
        "return_target": target,
    }


@dataclass(frozen=True, slots=True)
class _CheckedRequest:
    operation_id: str
    request_ids: tuple[str, ...]
    evidence: observations.CurrentEvidence
    owned: source_members.RequestOwnership | None
    phase: Phase
    grant: authority.AuthorityDecision | None
    reason: str


@dataclass(frozen=True, slots=True)
class ActiveGitHubRequests:
    grants: tuple[CurrentGitHubGrant, ...] = ()
    unavailable: bool = False
    cancellation: str | None = None


def _check_requests(
    conn,
    config,
    ticket,
    claim,
    *,
    revision,
    operations,
    facts,
    project,
    binding,
    now,
    active=None,
    target=None,
    final=False,
):
    accepted = []
    for operation_id, request_ids in operations:
        current = observations.current_batch(
            conn,
            config,
            claim,
            ticket,
            revision=revision,
            operation_id=operation_id,
            request_ids=request_ids,
            now=now,
            active=active,
            target=target,
        )
        expected = "github_current_grant" if final else None
        if current is None or any(checked.disposition != expected for checked in current):
            return None
        for checked in current:
            owned = source_members.owned_request(conn, ticket.source_id, checked.request_id)
            if (
                owned is None
                or owned.return_target is None
                or owned.request["phase"] not in (Phase.PENDING, Phase.HELD, Phase.PAUSED)
                or owned.events[0]["state"] != RequestEventState.PENDING
            ):
                accepted.append(
                    _CheckedRequest(operation_id, request_ids, checked, None, Phase.HELD, None, checked.reason)
                )
                continue
            repos = [
                repo
                for repo in config["repos"]
                if repo["full_name"] == owned.return_target["repository"] and repo.get("enabled", True) is True
            ]
            repo = repos[0] if len(repos) == 1 else None
            phase, grant, reason = _phase_and_grant(owned, checked, facts, project, binding, repo)
            accepted.append(_CheckedRequest(operation_id, request_ids, checked, owned, phase, grant, reason))
    return tuple(accepted)


def _apply_requests(conn, config, ticket, claim, checked, *, revision, now, active=None, target=None, mutate=True):
    result = []
    for item in checked:
        changes = {}
        owned, grant = item.owned, item.grant
        if mutate and owned is not None:
            if grant is not None:
                changes = {
                    "authorization": {
                        **(json_object(owned.request["authorization_json"]) or {}),
                        "capability": grant.capability,
                        "rule": grant.rule,
                        "repos": list(grant.repos),
                        "event_id": owned.return_target["event_id"],
                    },
                    "snapshot": _snapshot(owned, item.evidence),
                    "version": item.evidence.semantic_version,
                }
            if not write.replace_current_grant(conn, owned=owned, phase=item.phase, now=now, **changes):
                return None
            if grant is not None:
                promoted = source_members.owned_request(conn, ticket.source_id, item.evidence.request_id)
                if promoted is None or not source_members.bound_snapshot(
                    promoted, promoted.request["content_snapshot_json"], promoted.request["content_version"]
                ):
                    return None
                result.append(CurrentGitHubGrant(promoted.request, grant))

        if not observations.reseal_current(
            conn,
            config,
            claim,
            ticket,
            revision=revision,
            operation_id=item.operation_id,
            request_ids=item.request_ids,
            checked=item.evidence,
            disposition=item.reason,
            active=active,
            target=target,
        ):
            return None
    return tuple(result)


def promote_observed_requests(conn, config, ticket, claim, *, revision, operations, facts, project, binding, now):
    """Produce admission grants only from exact current batches in this source writer."""
    if not conn.in_transaction or source_sessions.active_round(conn, ticket.source_id) is not None:
        return None
    checked = _check_requests(
        conn,
        config,
        ticket,
        claim,
        revision=revision,
        operations=operations,
        facts=facts,
        project=project,
        binding=binding,
        now=now,
    )
    if checked is None:
        return None
    return _apply_requests(conn, config, ticket, claim, checked, revision=revision, now=now)


def refresh_active_requests(
    conn, config, ticket, claim, *, revision, active, operations, facts, project, binding, now, target=None
):
    """Apply one full active-round observation or exact positive revocations only."""
    if (
        not conn.in_transaction
        or tuple(request_id for _, request_ids in operations for request_id in request_ids) != active.request_ids
    ):
        return None
    checked = _check_requests(
        conn,
        config,
        ticket,
        claim,
        revision=revision,
        operations=operations,
        facts=facts,
        project=project,
        binding=binding,
        now=now,
        active=active,
        target=target,
    )
    if checked is None:
        return None
    cancellation = None
    unavailable = any(item.grant is None for item in checked)
    negatives = tuple(item for item in checked if item.phase in (Phase.REVOKED, Phase.PAUSED))
    if negatives:
        if (
            _apply_requests(
                conn, config, ticket, claim, negatives, revision=revision, now=now, active=active, target=target
            )
            is None
        ):
            return None
        cancellation = next((item.reason for item in negatives if item.phase == Phase.REVOKED), negatives[0].reason)
        return ActiveGitHubRequests(cancellation=cancellation)
    grants = _apply_requests(
        conn,
        config,
        ticket,
        claim,
        checked,
        revision=revision,
        now=now,
        active=active,
        target=target,
        mutate=not unavailable,
    )
    return ActiveGitHubRequests(grants, unavailable) if grants is not None else None


def current_execution_grants(conn, config, ticket, proof, *, facts, project, binding, now):
    """Re-read every sealed round operation and its actual persisted current member."""
    if tuple(request_id for _, ids in proof.operations for request_id in ids) != proof.active.request_ids:
        return None
    checked = _check_requests(
        conn,
        config,
        ticket,
        proof.claim,
        revision=proof.revision,
        operations=proof.operations,
        facts=facts,
        project=project,
        binding=binding,
        now=now,
        active=proof.active,
        target=proof.target,
        final=True,
    )
    if checked is None or any(item.grant is None or item.owned is None for item in checked):
        return None
    for item in checked:
        row = item.owned.request
        if (
            row["phase"] != Phase.PENDING
            or row["content_version"] != item.evidence.semantic_version
            or not (source_members.bound_snapshot(item.owned, row["content_snapshot_json"], row["content_version"]))
        ):
            return None
    return tuple(CurrentGitHubGrant(item.owned.request, item.grant) for item in checked)


@dataclass(frozen=True, slots=True)
class AcceptedDisposition:
    revoked: tuple[dict, ...]
    intact: tuple[dict, ...]


def current_disposition(conn, config, ticket, claim, root_id, proof, *, revision, now):
    """Validate sealed negative/intact evidence, separate from all action permissions."""
    if (
        not isinstance(proof, observations.RootDispositionProof)
        or not isinstance(proof.scope, observations.AcceptedDispositionScope)
        or proof.claim != claim
        or proof.revision != revision
        or proof.scope.root_id != root_id
        or proof.scope.source_id != ticket.source_id
        or time.monotonic() >= proof.deadline
        or proof.scope != observations.accepted_disposition_scope(conn, root_id)
        or tuple(request for _, ids in proof.operations for request in ids) != proof.scope.request_ids
    ):
        return None
    revoked, intact = [], []
    for operation, request_ids in proof.operations:
        current = observations.current_batch(
            conn,
            config,
            claim,
            ticket,
            revision=revision,
            now=now,
            operation_id=operation,
            request_ids=request_ids,
            delivery=proof.scope,
        )
        if current is None or any(item.disposition is not None for item in current):
            return None
        for item in current:
            owner = source_members.owned_request(conn, ticket.source_id, item.request_id)
            target = owner.return_target if owner else None
            if target is None:
                return None
            sealed = {
                "reason": item.reason,
                "request_id": item.request_id,
                "event_id": target["event_id"],
                "target_sha256": context_digest(target),
                "operation_id": operation,
                "observation_hash": context_digest(asdict(item)),
                "observed_at": item.observed_at,
            }
            if (
                item.reason in source_retirement.GITHUB_WITHDRAWAL_REASONS
                and isinstance(item.parent, dict)
                and item.status == ("absent" if item.reason == "github_item_absence_confirmed" else "present")
            ):
                revoked.append(sealed)
            elif (
                item.reason == "github_current_mention"
                and item.status == "present"
                and item.semantic_version is not None
                and isinstance(item.parent, dict)
                and _first_request_observed(json_object(owner.bridge["first_observation_json"]) or {}, item)
            ):
                intact.append(sealed)
    return AcceptedDisposition(tuple(revoked), tuple(intact)) if time.monotonic() < proof.deadline else None


def current_recovery_discussions(conn, config, ticket, claim, scope, proof, *, revision, now, expected_pr):
    """Prove all original current triggers without promoting or amending any request."""
    if type(proof) is not observations.RootRecoveryProof or type(scope) is not observations.AcceptedRecoveryScope:
        return None, "source_delivery_observation_unavailable"
    return _current_recovery_discussions(
        conn,
        config,
        ticket,
        claim,
        scope,
        proof,
        revision=revision,
        now=now,
        expected_pr=expected_pr,
    )


def current_recovered_tapd_discussions(conn, config, ticket, claim, scope, proof, *, revision, now, expected_pr):
    """Require only the operation dedicated to the exact recovered TAPD comment."""
    if type(proof) is not observations.RootRecoveredTapdProof or type(scope) is not observations.RecoveredTapdScope:
        return None, "source_delivery_observation_unavailable"
    return _current_recovery_discussions(
        conn,
        config,
        ticket,
        claim,
        scope,
        proof,
        revision=revision,
        now=now,
        expected_pr=expected_pr,
    )


def _current_recovery_discussions(conn, config, ticket, claim, scope, proof, *, revision, now, expected_pr):
    if (
        proof.scope != scope
        or proof.claim != claim
        or proof.revision != revision
        or time.monotonic() >= proof.deadline
        or tuple(request_id for _, batch in proof.operations for request_id in batch) != scope.request_ids
    ):
        return None, "source_delivery_observation_unavailable"
    sealed = []
    positive_drift = False
    for operation, ids in proof.operations:
        current = observations.current_batch(
            conn,
            config,
            claim,
            ticket,
            revision=revision,
            operation_id=operation,
            request_ids=ids,
            now=now,
            delivery=scope,
        )
        if current is None:
            return None, "source_delivery_observation_unavailable"
        for evidence in current:
            if evidence.disposition is not None:
                return None, "source_delivery_observation_unavailable"
            if evidence.reason in source_retirement.GITHUB_WITHDRAWAL_REASONS:
                continue
            owned = source_members.owned_request(conn, ticket.source_id, evidence.request_id)
            parent = evidence.parent
            if (
                owned is None
                or owned.return_target is None
                or owned.request["phase"] == Phase.REVOKED
                or any(event["state"] == RequestEventState.REVOKED for event in owned.events)
                or evidence.reason != "github_current_mention"
                or evidence.status != "present"
                or evidence.semantic_version is None
                or not isinstance(parent, dict)
                or not _first_request_observed(json_object(owned.bridge["first_observation_json"]) or {}, evidence)
            ):
                continue
            target = owned.return_target
            if (
                parent.get("state") != "open"
                or parent.get("base_branch") != parent.get("original_base_branch")
                or (
                    expected_pr is not None
                    and (target["repository"].casefold(), target["pr_number"])
                    == (expected_pr["repository"].casefold(), expected_pr["number"])
                    and not _matches_recovery_parent(parent, expected_pr)
                )
            ):
                positive_drift = True
                continue
            sealed.append(
                {
                    "reason": evidence.reason,
                    "request_id": evidence.request_id,
                    "event_id": target["event_id"],
                    "target_sha256": context_digest(target),
                    "operation_id": operation,
                    "observation_hash": context_digest(asdict(evidence)),
                    "observed_at": evidence.observed_at,
                }
            )
    if positive_drift:
        return None, "source_delivery_reprocess_required"
    if len(sealed) != len(scope.request_ids) or time.monotonic() >= proof.deadline:
        return None, "source_delivery_observation_unavailable"
    return sealed, None


def _matches_recovery_parent(parent, expected):
    return (
        str(parent.get("url")).casefold() == expected["url"].casefold()
        and str(parent.get("base_repository")).casefold() == expected["repository"].casefold()
        and all(
            parent.get(field) == expected[field]
            for field in ("number", "author", "head_repository", "head_branch", "head_sha", "base_branch", "base_sha")
        )
    )


def revoke_unhandled_disposition(conn, ticket, checked, *, now):
    """Apply confirmed withdrawal only to still-pending ledger rows."""
    if checked is None:
        return
    for fact in checked.revoked:
        owner = source_members.owned_request(conn, ticket.source_id, fact["request_id"])
        if (
            owner is not None
            and owner.return_target is not None
            and context_digest(owner.return_target) == fact["target_sha256"]
            and owner.events[0]["state"] == RequestEventState.PENDING
            and owner.request["phase"] != Phase.REVOKED
            and not write.replace_current_grant(conn, owned=owner, phase=Phase.REVOKED, now=now)
        ):
            raise ValueError("Unhandled original GitHub member changed during revocation")


@dataclass(frozen=True, slots=True)
class CurrentGitHubDelivery:
    decisions: dict
    cancellation: str | None = None


def delivery_current_grants(conn, config, ticket, proof, *, facts, project, binding, now):
    """Recheck exact accepted-root operations and derive decisions without changing ingress."""
    scope = proof.scope
    if isinstance(proof, observations.RootDeliveryProof) and isinstance(scope, observations.AcceptedRootScope):
        code_effect = scope.code_effect
    elif isinstance(proof, observations.RootCompletionProof) and isinstance(
        scope, observations.AcceptedCompletionScope
    ):
        code_effect = None
    else:
        return None
    if tuple(request_id for _, batch in proof.operations for request_id in batch) != scope.request_ids:
        return None
    root = source_results.by_id(conn, scope.root_id)
    round_ = source_sessions.active_round(conn, scope.source_id)
    if root is None or round_ is None:
        return None
    owners = {
        member.member["request_id"]: member.ownership
        for member in source_members.accepted_round_members(conn, root, round_)
        if member.ownership.return_target is not None
    }
    decisions = {}
    negatives = []
    for operation, request_ids in proof.operations:
        current = observations.current_batch(
            conn,
            config,
            proof.claim,
            ticket,
            revision=proof.revision,
            now=now,
            operation_id=operation,
            request_ids=request_ids,
            delivery=scope,
        )
        if current is None or any(item.disposition is not None for item in current):
            return None
        for item in current:
            owned = owners.get(item.request_id)
            if owned is None:
                return None
            if item.reason == "github_current_creator_changed":
                negatives.append((Phase.REVOKED, item.reason))
                continue
            repo = observations.configured_repository(conn, config, ticket, owned.request["repo_id"])
            phase, decision, reason = _phase_and_grant(owned, item, facts, project, binding, repo)
            if phase in (Phase.REVOKED, Phase.PAUSED):
                negatives.append((phase, reason))
            elif decision is not None and observations.code_parent_current(code_effect, item.parent):
                decisions[item.request_id] = decision
    cancellation = next((reason for phase, reason in negatives if phase == Phase.REVOKED), None)
    return CurrentGitHubDelivery(decisions, cancellation or (negatives[0][1] if negatives else None))
