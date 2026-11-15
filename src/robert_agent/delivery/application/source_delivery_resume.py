"""Prove and apply the exact missing-effect continuation of a paused result."""

import json
import re
import subprocess
import time
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from functools import partial
from hashlib import sha256
from pathlib import Path

from robert_agent.adapters.github.pull_requests import read_native_prs, read_pr
from robert_agent.delivery.application import native_preparation, source_github_remote
from robert_agent.delivery.application import scope as delivery_scope
from robert_agent.foundation import values
from robert_agent.foundation.commands import ControlDeadlineExceeded, run_command
from robert_agent.foundation.contracts.delivery import PublishStatus
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import (
    source_delivery_pauses as pauses,
)
from robert_agent.persistence.queries import (
    source_delivery_recovery as read,
)
from robert_agent.persistence.queries import (
    source_github_publication as github,
)
from robert_agent.persistence.queries import (
    source_members,
    source_pr_lineage,
    source_results,
    source_retirement,
)
from robert_agent.persistence.queries import (
    source_pr_selection as selection,
)
from robert_agent.persistence.queries import (
    source_sessions as sessions,
)
from robert_agent.persistence.rounds.source_evidence import context_digest, json_object
from robert_agent.persistence.stores import source_delivery_recovery as write
from robert_agent.persistence.stores import source_sessions as fence
from robert_agent.work import source_attention, tapd_authority
from robert_agent.work import source_pr_selection as pr_policy
from robert_agent.work.application import (
    source_confirmations,
    source_github_observation,
    source_request_authority,
    tapd_intake,
)

_MAX_PROMPT_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PullRequestProof:
    repository: str
    number: int
    author: str
    head_repository: str
    head_branch: str
    head_sha: str
    base_branch: str
    base_sha: str
    url: str


@dataclass(frozen=True, slots=True)
class BaseReferenceProof:
    repository: str
    branch: str
    sha: str


@dataclass(frozen=True, slots=True)
class RecoveryCheck:
    claim: fence.SourceClaim
    revision: str
    source_id: str
    root_id: str
    generation: int
    transition_id: str
    durable: read.BoundDelivery
    audit_scope: dict | None
    pull_request: PullRequestProof | None
    question_id: str
    action_id: str
    base_reference: BaseReferenceProof | None
    selection: dict | None
    creation_history: tuple
    readonly_snapshot: dict | None
    receipt: source_confirmations.ConfirmationReceipt | None = None
    discussion: source_github_observation.AcceptedRecoveryScope | None = None


@dataclass(frozen=True, slots=True)
class RecoveryObservation:
    check: RecoveryCheck
    prompt_sha256: str
    pull_request: PullRequestProof | None
    base_reference: BaseReferenceProof | None
    discussion: source_github_observation.RootRecoveryProof | None = None
    tapd_corroboration: tapd_authority.TicketObservation | None = None


def _check(conn, pause, durable, transition_id, question_id, *, claim, revision, receipt=None, action_id=None):
    root = source_results.by_id(conn, pause["source_result_id"])
    actions = source_results.required_actions(conn, pause["source_result_id"])
    sets = source_results.required_action_sets(conn, root, actions) if root is not None else None
    if sets is None:
        return None
    base_actions, _feedback_actions = sets
    pending = [action for action in actions if action["publish_status"] != PublishStatus.PUBLISHED]
    selected = (
        next((action for action in actions if action["action_id"] == action_id), None)
        if action_id
        else (pending or actions)[0]
    )
    if selected is None or (pending and selected["action_id"] != pending[0]["action_id"]):
        return None
    discussion = None
    if source_members.round_github_request_ids(conn, pause["round_id"]):
        discussion = source_github_observation.accepted_recovery_scope(
            conn,
            pause["source_result_id"],
            generation=pause["generation"],
            question_id=question_id,
            transition_id=transition_id,
            action_id=selected["action_id"],
        )
        if discussion is None or discussion.purpose != ("claim" if receipt is not None else "owned"):
            return None
    github_actions = [action for action in base_actions if action["provider"] == "github"]
    selected_code = selected in github_actions
    if not github_actions:
        candidate = source_results.worker_candidate(conn, root["worker_result_id"])
        metadata = json_object(candidate["metadata_json"]) if candidate is not None else None
        snapshot = ((metadata or {}).get("audit") or {}).get("native_tapd", {}).get("readonly_snapshot")
        repository = (json_object(pause["proof_json"]) or {}).get("scope", {}).get("repository", {})
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("root") != repository.get("repo_root")
            or snapshot.get("repository") != repository.get("full_name")
        ):
            return None
        return RecoveryCheck(
            claim,
            revision,
            pause["source_id"],
            pause["source_result_id"],
            pause["generation"],
            transition_id,
            durable,
            None,
            None,
            question_id,
            selected["action_id"],
            None,
            None,
            (),
            snapshot,
            receipt,
            discussion,
        )
    effect = selected if selected_code else github_actions[-1]
    audit = delivery_scope.trusted_action_scope(conn, dict(effect), include_output_type=True)
    if not isinstance(audit, dict):
        return None
    pr, base, decision, history = None, None, None, ()
    try:
        purpose = audit["native_github"]["purpose"]
        pin = purpose["git_pin"]
        if selected_code:
            row = github.required_action(conn, pause["source_result_id"], selected["action_id"])
            if row is None or native_preparation.github_effect_metadata(dict(row), audit) is None:
                return None
            valid, decision = selection.effect_selection(conn, row)
            history = selection.creation_history(conn, pause["source_id"], row["repo_id"]) if decision else ()
            if not valid or history is None:
                return None
            if purpose["mode"] == "new_branch":
                base = BaseReferenceProof(pin["repository"], pin["base_branch"], pin["base_sha"])
            elif purpose["mode"] == "existing_pr":
                original = purpose["pr"]
                lineage = source_pr_lineage.confirmed_pr_lineage(
                    conn,
                    source_id=pause["source_id"],
                    repo_id=row["repo_id"],
                    workstream_id=original["workstream_id"],
                    action_id=original["action_id"],
                )
                if lineage is None:
                    return None
                pr = PullRequestProof(
                    lineage.repository,
                    original["number"],
                    lineage.author,
                    original["head_repository"],
                    original["branch_name"],
                    original["head_sha"],
                    original["base_branch"],
                    original["base_sha"],
                    lineage.url,
                )
            else:
                return None
        else:
            confirmed = github.confirmed_pr(conn, pause["source_result_id"], effect["action_id"])
            if confirmed is None:
                return None
            _, _packet, facts = confirmed
            pr = PullRequestProof(
                facts["base_repository"],
                facts["number"],
                facts["author"],
                facts["head_repository"],
                facts["head_branch"],
                facts["head_sha"],
                facts["base_branch"],
                pin["base_sha"],
                facts["url"],
            )
    except (KeyError, TypeError):
        return None
    return RecoveryCheck(
        claim,
        revision,
        pause["source_id"],
        pause["source_result_id"],
        pause["generation"],
        transition_id,
        durable,
        audit,
        pr,
        question_id,
        selected["action_id"],
        base,
        decision,
        tuple(history),
        None,
        receipt,
        discussion,
    )


def _dormant_candidates(conn, ticket):
    return [
        pause
        for pause in pauses.latest_in_scope(conn, (), ticket.source_id)
        if read.for_generation(conn, pause["source_result_id"], pause["generation"]) is None
        and not source_retirement.is_retired(conn, pause["source_result_id"])
    ]


def proposal(conn, config, ticket, claim, *, revision):
    """Materialize an exact dormant candidate and choice before any external I/O."""
    if claim.source_id != ticket.source_id or not revision:
        return None, None
    offer = tapd_intake.same_rule_resume_offer(conn, config, ticket, revision=revision)
    if offer is None:
        return None, None
    candidates = _dormant_candidates(conn, ticket)
    if not candidates:
        return None, None
    if (
        len(candidates) != 1
        or sessions.active_round(conn, ticket.source_id) is not None
        or sessions.blockers_for_source(conn, ticket.source_id, limit=1)
    ):
        return None, "source_delivery_recovery_unsettled"
    pause = candidates[0]
    missing = pauses.dormant_actions(conn, pause)
    durable = read.bound_delivery(conn, pause)
    root = source_results.by_id(conn, pause["source_result_id"])
    round_ = source_results.delivery_round(conn, ticket.source_id, pause["round_id"])
    mixed = bool(root and round_ and source_members.round_github_request_ids(conn, round_["round_id"]))
    saved = (json_object(pause["proof_json"]) or {}).get("scope")
    if mixed:
        current = tapd_intake.mixed_same_rule_pause(conn, config, ticket, root, round_, revision=revision)
        original_scope = (
            {key: value for key, value in saved.items() if key != "github_disposition"}
            if isinstance(saved, dict)
            else None
        )
    else:
        members = source_results.consumed_members(conn, root, round_) if root and round_ else ()
        current = tapd_intake.paused_delivery_scope(conn, config, ticket, members, revision=revision)
        original_scope = saved
    if durable is None or missing != durable.missing or current is None or current != original_scope:
        return None, "source_delivery_reprocess_required"
    receipt = offer.receipt
    transition_id = source_confirmations.resume_identity(
        ticket.source_id, receipt.question_id, receipt.version, receipt.scope_digest
    )
    checked = _check(
        conn, pause, durable, transition_id, receipt.question_id, claim=claim, revision=revision, receipt=receipt
    )
    if checked is None:
        sets = source_results.required_action_sets(
            conn, root, source_results.required_actions(conn, pause["source_result_id"])
        )
        if sets is not None and not any(action["provider"] == "github" for action in sets[0]):
            return None, "source_delivery_original_checkout_unproven"
    return (checked, None) if checked is not None else (None, "source_delivery_reprocess_required")


def _current_pull_request(facts):
    try:
        state, merged = facts["state"], facts["merged"]
        if state not in ("open", "closed") or type(merged) is not bool:
            return None, None
        head, base = facts["head"], facts["base"]
        pr = PullRequestProof(
            base["repo"]["full_name"].casefold(),
            facts["number"],
            facts["user"]["login"].casefold(),
            head["repo"]["full_name"].casefold(),
            head["ref"],
            head["sha"],
            base["ref"],
            base["sha"],
            facts["html_url"].casefold(),
        )
        if (
            type(pr.number) is not int
            or pr.number <= 0
            or any(not isinstance(value, str) or not value for key, value in asdict(pr).items() if key != "number")
            or not all(re.fullmatch(r"[0-9a-f]{40}", digest) for digest in (pr.head_sha, pr.base_sha))
        ):
            return None, None
        return pr, state == "open" and not merged and facts.get("merged_at") is None
    except (TypeError, AttributeError, KeyError):
        return None, None


def _local_artifacts_match(check):
    try:
        with Path(check.durable.prompt_path).open("rb") as handle:
            prompt = handle.read(_MAX_PROMPT_BYTES + 1)
    except OSError:
        return False
    return (
        len(prompt) <= _MAX_PROMPT_BYTES
        and sha256(prompt).hexdigest() == check.durable.prompt_sha256
        and (check.audit_scope is None or native_preparation.recheck_github(check.audit_scope))
        and (check.readonly_snapshot is None or native_preparation.recheck_readonly(check.readonly_snapshot))
    )


def _ticket_facts(snapshot):
    material = asdict(snapshot)
    material.pop("observed_at")
    material["comments"] = sorted(material["comments"], key=context_digest)
    material["absences"] = sorted(material["absences"], key=context_digest)
    return material


def same_recovered_tapd_snapshot(first, later):
    """Independently corroborate current unchanged provider facts after GitHub I/O."""
    return (
        isinstance(first, tapd_authority.TicketObservation)
        and isinstance(later, tapd_authority.TicketObservation)
        and utc_timestamp(first.observed_at) is not None
        and utc_timestamp(later.observed_at) is not None
        and utc_timestamp(first.observed_at) < utc_timestamp(later.observed_at)
        and first.coverage == later.coverage == "complete"
        and _ticket_facts(first) == _ticket_facts(later)
    )


def is_recovered_tapd_observation(observation):
    return isinstance(observation, RecoveryObservation) and isinstance(
        observation.check.discussion,
        source_github_observation.RecoveredTapdScope,
    )


def recovered_tapd_snapshot_current(observation, initial):
    return (
        is_recovered_tapd_observation(observation)
        and observation.check.discussion.snapshot_sha256 == context_digest(asdict(initial))
        and same_recovered_tapd_snapshot(initial, observation.tapd_corroboration)
    )


def recovered_tapd_is_current(observation):
    return (
        is_recovered_tapd_observation(observation)
        and type(observation.discussion) is source_github_observation.RootRecoveredTapdProof
        and time.monotonic() < observation.discussion.deadline
    )


def corroborate_recovered_tapd(observation, snapshot):
    """Reprove the original local artifacts after the final outside-writer provider read."""
    if not recovered_tapd_is_current(observation):
        return None, "source_grant_unavailable"
    if not _local_artifacts_match(observation.check):
        return None, "source_delivery_reprocess_required"
    return replace(observation, tapd_corroboration=snapshot), None


def observe(check, *, runner=None, stop=None, observe_discussion=None):
    """Reprove local accepted artifacts and the current exact public PR outside a writer."""
    if stop is not None and stop.is_set():
        return None, "source_delivery_stopped"
    if not _local_artifacts_match(check):
        return None, "source_delivery_reprocess_required"
    current = None
    if check.pull_request is not None:
        expected = check.pull_request
        try:
            raw = read_pr(expected.repository, expected.number, host="github.com", runner=runner, stop=stop, timeout=10)
        except ControlDeadlineExceeded:
            raise
        except (ValueError, OSError, subprocess.SubprocessError):
            return None, "source_delivery_observation_unavailable"
        current, opened = _current_pull_request(raw)
        if current is None:
            return None, "source_delivery_observation_unavailable"
        if not opened or current != expected:
            return None, "source_delivery_reprocess_required"
    base = None
    try:
        if check.base_reference is not None:
            expected = check.base_reference
            observed = source_github_remote.read_base_reference(
                partial(run_command, runner=runner), expected.repository, expected.branch, stop=stop
            )
            if observed is None or observed["sha"] != expected.sha:
                return None, "source_delivery_reprocess_required"
            base = expected
        if check.selection is not None:
            snapshots = read_native_prs(
                check.selection["repository"],
                (entry.number for entry in check.creation_history),
                runner=runner,
                stop=stop,
            )
            observations = pr_policy.normalize_observations(
                check.creation_history, snapshots, check.selection["publisher"]
            )
            if not pr_policy.effect_matches_selection(check.selection, check.creation_history, observations):
                return None, "source_delivery_reprocess_required"
    except ControlDeadlineExceeded:
        raise
    except (ValueError, OSError, TypeError, KeyError, subprocess.SubprocessError):
        return None, "source_delivery_observation_unavailable"
    if stop is not None and stop.is_set():
        return None, "source_delivery_stopped"
    discussion = None
    if check.discussion is not None:
        if not callable(observe_discussion):
            return None, "source_delivery_observation_unavailable"
        discussion = observe_discussion(check.discussion)
        if discussion is None:
            return None, "source_delivery_observation_unavailable"
        if not _local_artifacts_match(check):
            return None, "source_delivery_reprocess_required"
    return RecoveryObservation(check, check.durable.prompt_sha256, current, base, discussion), None


def _current_mixed_discussions(conn, config, ticket, claim, check, observation, *, now):
    scope = check.discussion
    return source_request_authority.current_recovery_discussions(
        conn,
        config,
        ticket,
        claim,
        scope,
        observation.discussion,
        revision=check.revision,
        now=now,
        expected_pr=asdict(check.pull_request) if check.pull_request is not None else None,
    )


def apply(conn, config, ticket, claim, observation, *, revision, now):
    if not conn.in_transaction or claim.source_id != ticket.source_id or not fence.lock_claim(conn, claim, now=now):
        return None
    check, _ = proposal(conn, config, ticket, claim, revision=revision)
    if check is None or not _matches(check, observation):
        return None
    pause = pauses.latest(conn, check.root_id)
    github_evidence = {}
    if check.discussion is not None:
        original, reason = _current_mixed_discussions(conn, config, ticket, claim, check, observation, now=now)
        if original is None:
            if reason == "source_delivery_reprocess_required":
                source_attention.open_delivery_recovery(conn, pause, reason=reason, now=now)
            return None
        github_evidence["original_github"] = {
            "version": 1,
            "scope_sha256": check.discussion.commitment,
            "members": original,
        }
    checkpoint = "apply_accepted_delivery_resume"
    transactions.savepoint(conn, checkpoint)
    try:
        started = write.start(
            conn,
            pause=pause,
            question_id=check.question_id,
            transition_id=check.transition_id,
            binding_sha256=check.durable.digest,
            evidence={
                "prompt_sha256": observation.prompt_sha256,
                "pull_request": asdict(observation.pull_request) if observation.pull_request else None,
                "base_reference": asdict(observation.base_reference) if observation.base_reference else None,
                **github_evidence,
            },
            now=now,
        )
        result = (
            tapd_intake.apply_delivery_resume(
                conn,
                config,
                ticket,
                claim,
                source_result_id=check.root_id,
                generation=check.generation,
                revision=revision,
                now=now,
            )
            if started
            else None
        )
        if result is not None:
            source_attention.resolve_delivery_recovery(conn, ticket.source_id, check.root_id, check.generation, now=now)
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.finish_isolation(conn, checkpoint, rollback=result is None)
    return result


def record_stale(db_path, config, ticket, check, reason):
    """Bind an external stale-proof observation to its still-current local owner."""
    if reason not in {"source_delivery_reprocess_required", "source_delivery_original_checkout_unproven"}:
        return False
    with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
        transactions.begin_immediate(conn)
        now = values.utc_now()
        if ticket.source_id != check.source_id or not fence.lock_claim(conn, check.claim, now=now):
            return False
        source = source_results.tapd_source(conn, ticket.source_id)
        if source is None or (json_object(source["metadata_json"]) or {}).get("revision") != check.revision:
            return False
        if check.receipt is not None:
            current = proposal(conn, config, ticket, check.claim, revision=check.revision)[0]
        elif isinstance(check.discussion, source_github_observation.RecoveredTapdScope):
            current = _recovered_tapd_check(
                conn,
                config,
                ticket,
                check.claim,
                check.root_id,
                check.action_id,
                revision=check.revision,
                snapshot_sha256=check.discussion.snapshot_sha256,
            )
        else:
            current = _same_owned_check(
                conn, config, ticket, check.claim, check.root_id, check.action_id, check.revision
            )
        if current != check:
            return False
        pause = pauses.latest(conn, check.root_id)
        return source_attention.open_delivery_recovery(conn, pause, reason=reason, now=now)


def record_unproven(conn, config, ticket, claim, *, revision, reason, now, root_id=None, action_id=None):
    """Report a proven local recovery owner whose original evidence cannot be rebuilt."""
    if (
        reason not in {"source_delivery_reprocess_required", "source_delivery_original_checkout_unproven"}
        or not conn.in_transaction
        or claim.source_id != ticket.source_id
        or not fence.lock_claim(conn, claim, now=now)
    ):
        return False
    source = source_results.tapd_source(conn, ticket.source_id)
    if not revision or source is None or (json_object(source["metadata_json"]) or {}).get("revision") != revision:
        return False
    if action_id is not None:
        owner = read.active(conn, ticket.source_id)
        if (
            owner is None
            or owner["source_result_id"] != root_id
            or _owned_check(conn, claim, owner, action_id, revision=revision) is not None
        ):
            return False
        pause = pauses.latest(conn, root_id)
        if pause is None or pause["generation"] != owner["generation"]:
            return False
        if action_id not in {row["action_id"] for row in source_results.required_actions(conn, root_id)}:
            return False
    else:
        check, current_reason = proposal(conn, config, ticket, claim, revision=revision)
        candidates = _dormant_candidates(conn, ticket)
        if check is not None or current_reason != reason or len(candidates) != 1:
            return False
        pause = candidates[0]
    return source_attention.open_delivery_recovery(conn, pause, reason=reason, now=now)


def _matches(check, observation):
    return (
        isinstance(observation, RecoveryObservation)
        and observation.check == check
        and observation.prompt_sha256 == check.durable.prompt_sha256
        and observation.pull_request == check.pull_request
        and observation.base_reference == check.base_reference
        and ((check.discussion is None) == (observation.discussion is None))
    )


def effect_check(conn, claim, root_id, action_id, *, revision):
    """Load the next missing effect from this exact active recovery owner."""
    owner = read.active(conn, claim.source_id)
    if not revision or owner is None or owner["source_result_id"] != root_id:
        return None
    pause = pauses.latest(conn, root_id)
    if pause is None or source_members.round_github_request_ids(conn, pause["round_id"]):
        return None
    return _owned_check(conn, claim, owner, action_id, revision=revision)


def _owned_check(conn, claim, owner, action_id, *, revision):
    root_id = owner["source_result_id"]
    durable = read.applied_bound(conn, owner)
    if durable is None or action_id not in durable.missing:
        return None
    action = next(
        (row for row in source_results.required_actions(conn, root_id) if row["action_id"] == action_id), None
    )
    if (
        action is None
        or action["provider"] not in ("tapd", "github")
        or action["publish_status"] != PublishStatus.NOT_PUBLISHED
    ):
        return None
    return _check(
        conn,
        pauses.latest(conn, root_id),
        durable,
        owner["transition_id"],
        owner["question_id"],
        claim=claim,
        revision=revision,
        action_id=action_id,
    )


def active_proposal(conn, config, ticket, claim, *, revision, now):
    """Check an already-owned recovery even when an effect's preflight cannot proceed."""
    owner = read.active(conn, ticket.source_id)
    if (
        owner is None
        or tapd_intake.current_delivery_transition(conn, config, ticket, revision=revision) != owner["transition_id"]
    ):
        return None, None
    actions = source_results.required_actions(conn, owner["source_result_id"])
    pending = [action for action in actions if action["publish_status"] != PublishStatus.PUBLISHED]
    # An already-started unknown effect retains reconciliation ownership. This
    # check reports stale original evidence; it does not settle remote effects.
    if not pending or any(action["intent_started_at"] is not None for action in pending):
        return None, None
    action_id = pending[0]["action_id"]
    check = _owned_check(conn, claim, owner, action_id, revision=revision)
    if check is not None:
        return check, None
    reason = "source_delivery_reprocess_required"
    record_unproven(
        conn,
        config,
        ticket,
        claim,
        revision=revision,
        reason=reason,
        now=now,
        root_id=owner["source_result_id"],
        action_id=action_id,
    )
    return None, reason


def _same_owned_check(conn, config, ticket, claim, root_id, action_id, revision):
    owner = read.active(conn, ticket.source_id)
    if (
        owner is None
        or owner["source_result_id"] != root_id
        or tapd_intake.current_delivery_transition(conn, config, ticket, revision=revision) != owner["transition_id"]
    ):
        return None
    return _owned_check(conn, claim, owner, action_id, revision=revision)


def recovered_tapd_check(conn, config, ticket, claim, root_id, action_id, *, revision, snapshot):
    return _recovered_tapd_check(
        conn,
        config,
        ticket,
        claim,
        root_id,
        action_id,
        revision=revision,
        snapshot_sha256=context_digest(asdict(snapshot)),
    )


def _recovered_tapd_check(conn, config, ticket, claim, root_id, action_id, *, revision, snapshot_sha256):
    original = _same_owned_check(conn, config, ticket, claim, root_id, action_id, revision)
    if original is None or original.discussion is None:
        return None
    distinct = source_github_observation.recovered_tapd_scope(conn, original.discussion, snapshot_sha256)
    return replace(original, discussion=distinct) if distinct is not None else None


def current_recovered_tapd(
    conn, config, ticket, claim, root, round_, action, observation, *, revision, now, repo_names, repo_id
):
    """Only the next missing TAPD comment, after current authorization of all original triggers."""
    if (
        not recovered_tapd_is_current(observation)
        or not conn.in_transaction
        or observation.tapd_corroboration is None
        or claim.source_id != ticket.source_id
        or not fence.lock_claim(conn, claim, now=now)
    ):
        return ()
    expected = _recovered_tapd_check(
        conn,
        config,
        ticket,
        claim,
        root["source_result_id"],
        action["action_id"],
        revision=revision,
        snapshot_sha256=observation.check.discussion.snapshot_sha256,
    )
    if expected is None or not _matches(expected, observation):
        return ()
    checked, reason = source_request_authority.current_recovered_tapd_discussions(
        conn,
        config,
        ticket,
        claim,
        expected.discussion,
        observation.discussion,
        revision=revision,
        now=now,
        expected_pr=asdict(expected.pull_request) if expected.pull_request is not None else None,
    )
    if checked is None:
        if reason == "source_delivery_reprocess_required":
            source_attention.open_delivery_recovery(conn, pauses.latest(conn, expected.root_id), reason=reason, now=now)
        return ()
    members = source_members.disposition_members(conn, root, round_)
    if not members:
        return ()
    original = (json_object(round_["input_snapshot_json"]) or {}).get("authority", {})
    tapd_members = tuple(
        {**dict(item.ownership.request), "authorization_json": json.dumps(original[item.member["request_id"]])}
        for item in members
        if item.ownership.return_target is None
    )
    decisions = tapd_intake.delivery_request_decisions(conn, config["tapd"], ticket, tapd_members, revision=revision)
    if (
        len(decisions) != len(tapd_members)
        or any(
            decision.reason != "allowed"
            or decision.capability != round_["capability"]
            or not repo_names <= set(decision.repos)
            or row["repo_id"] not in (None, repo_id)
            for row, decision in zip(tapd_members, decisions, strict=True)
        )
        or not recovered_tapd_is_current(observation)
    ):
        return ()
    return tuple(item.member["request_id"] for item in members)


def current_effect(conn, config, ticket, root_id, action_id, observation, *, revision, claim):
    if claim is None or claim.source_id != ticket.source_id:
        return False
    check = effect_check(conn, claim, root_id, action_id, revision=revision)
    return (
        check is not None
        and _matches(check, observation)
        and tapd_intake.current_delivery_transition(conn, config, ticket, revision=revision) == check.transition_id
    )


def confirm_active(conn, config, ticket, claim, observation, *, revision, now):
    """Clear stale-evidence attention only after this already-owned recovery is revalidated."""
    if (
        not conn.in_transaction
        or not isinstance(observation, RecoveryObservation)
        or observation.check.receipt is not None
        or not fence.lock_claim(conn, claim, now=now)
    ):
        return False
    check = observation.check
    current = _same_owned_check(conn, config, ticket, claim, check.root_id, check.action_id, revision)
    if current is None or not _matches(current, observation):
        return False
    if current.discussion is not None:
        original, reason = _current_mixed_discussions(conn, config, ticket, claim, current, observation, now=now)
        if original is None:
            if reason == "source_delivery_reprocess_required":
                source_attention.open_delivery_recovery(
                    conn, pauses.latest(conn, check.root_id), reason=reason, now=now
                )
            return False
    source_attention.resolve_delivery_recovery(conn, ticket.source_id, check.root_id, check.generation, now=now)
    return True
