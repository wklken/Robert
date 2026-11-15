"""Perform or reconcile one accepted native GitHub publication effect."""

from collections.abc import Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
import re
import subprocess

from robert_agent.adapters.github import pull_requests
from robert_agent.adapters.tapd.client import TapdClient, TapdError
from robert_agent.delivery.application import effects, guards, native_preparation, scope, source_publication_queue
from robert_agent.delivery.application import (
    source_github_remote as remote,
    source_results as authority,
    source_delivery_observation,
    source_delivery_resume,
)
from robert_agent.delivery.lock import source_publication_lock, task_publication_lock
from robert_agent.foundation import values
from robert_agent.foundation.commands import ControlDeadlineExceeded, run_command as control_command
from robert_agent.foundation.contracts.delivery import GitHubActionType, PublicationStatus, PublishStatus
from robert_agent.persistence import database, errors, execution_control, transactions
from robert_agent.persistence.queries import source_attention as attention_read
from robert_agent.persistence.queries import (
    source_github_publication as read,
    source_members,
    source_publication as schedule,
    source_pr_selection as selection,
    source_results,
    source_delivery_recovery,
    source_sessions,
)
from robert_agent.persistence.rounds.source_evidence import context_digest
from robert_agent.persistence.stores import source_github_publication as write, source_sessions as fence
from robert_agent.work.tapd_authority import TicketIdentity
from robert_agent.work import source_attention, source_pr_selection as pr_policy
from robert_agent.work.application.source_observation import read_ticket_snapshot
from robert_agent.work.application import source_github_observation, tapd_intake, source_observation

_STARTED = {PublicationStatus.EXECUTING, PublicationStatus.ATTENTION}
_MAX_COMMAND_SECONDS = 20
_LEASE_SECONDS = 300
_REMOTE_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError)
_REPO = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*/[a-zA-Z0-9_.-]+")
_LOGIN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,38}")
_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True, slots=True)
class GitHubPublicationOutcome:
    status: str
    reason: str
    receipt: read.ConfirmedGitHubEffect | None = None


@contextmanager
def _writer(db_path):
    with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
        transactions.begin_immediate(conn)
        yield conn


def _packet(conn, row):
    identity = read.request_identity(row)
    action = dict(row)
    audit_scope = scope.trusted_action_scope(conn, action, include_output_type=True)
    resolved = native_preparation.github_effect_metadata(action, audit_scope)
    if identity is None or resolved is None:
        return None
    publisher = audit_scope.get("github_account")
    if not isinstance(publisher, str) or not _LOGIN.fullmatch(publisher):
        return None
    if not all(_REPO.fullmatch(resolved[field]) for field in ("repo", "head_repository")):
        return None
    if not all(_SHA.fullmatch(resolved[field]) for field in ("head_sha", "base_sha")):
        return None
    fingerprint = identity["submission_fingerprint"]
    if not isinstance(fingerprint, str) or not re.fullmatch("[0-9a-f]{64}", fingerprint):
        return None
    packet = {
        "version": 1,
        "identity": identity,
        "publisher": publisher.casefold(),
        "effect": resolved,
        "audit_scope_sha256": context_digest(audit_scope),
    }
    if row["action_type"] == GitHubActionType.OPEN_PR:
        marker = f"<!-- robert-workstream effect={context_digest(identity)[:32]} -->"
        resolved["body"] = f"{resolved['body']}\n\n{marker}"
        packet.update(marker=marker, body_sha256=sha256(resolved["body"].encode()).hexdigest())
    return packet, audit_scope


def _projection(conn, row):
    try:
        return _packet(conn, row)
    except (KeyError, TypeError, ValueError):
        return None


def _predecessors(conn, row):
    prefix = source_results.confirmed_required_effects(conn, row["root_id"], None, before_position=row["position"])
    return prefix is not None and all(prior["provider"] == "github" for prior in prefix)


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


def _runner(run_command, stop):
    def run(command, **kwargs):
        if stop is not None and stop.is_set():
            raise ControlDeadlineExceeded(command[:2], 0)
        # Git and GH tool configuration stays available; inherited variables
        # cannot substitute a repository, host or working tree.
        env = {
            key: val
            for key, val in os.environ.items()
            if not key.startswith("GIT_") and key not in {"GH_HOST", "GH_REPO", "GH_DEBUG", "GH_FORCE_TTY"}
        }
        env.update(GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1", GH_PROMPT_DISABLED="1", GH_HOST="github.com")
        if command[:2] == ["gh", "api"] and command[2:4] != ["--hostname", "github.com"]:
            command = [*command[:2], "--hostname", "github.com", *command[2:]]
        kwargs.pop("env", None)
        timeout = min(kwargs.pop("timeout", None) or _MAX_COMMAND_SECONDS, _MAX_COMMAND_SECONDS)
        return control_command(command, runner=run_command, timeout=timeout, env=env, **kwargs)

    return run


def _start(db_path, config, ticket, claim, root_id, action_id, snapshot, packet, stop, recovery, mixed=None):
    fingerprint = context_digest(packet)
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
            recovery_observation=recovery,
            mixed_observation=mixed,
            code_effect=source_github_observation.code_prestart(packet),
        )
        row = read.required_action(conn, root_id, action_id)
        if (
            grant is None
            or row is None
            or (stop is not None and stop.is_set())
            or source_results.unknown_github_effect(conn, ticket.source_id, require_positive_receipt=True)
            or source_sessions.blockers_for_publication(conn, ticket.source_id, root_id)
            or not _predecessors(conn, row)
            or not selection.effect_selection(conn, row)[0]
            or (current := _projection(conn, row)) is None
            or current[0] != packet
            or (mixed is not None and not source_delivery_observation.is_current(mixed))
        ):
            return None
        if row["publication_id"] is not None and read.prepared_request(row) != packet:
            return None
        intent = execution_control.prepare_publication_intent(
            conn, action_id, f"native-github:{action_id}", fingerprint, now
        )
        if row["publication_id"] is None:
            write.insert_preparation(
                conn,
                source_result_id=root_id,
                action_id=action_id,
                publication_id=intent["publication_id"],
                packet=packet,
            )
        execution_control.transition_publication_intent(
            conn, intent["publication_id"], PublicationStatus.EXECUTING, now
        )
        return intent["publication_id"], fingerprint, grant.revision


def _settle(db_path, claim, root_id, action_id, publication_id, fingerprint, proof, observed_at, stop):
    with _writer(db_path) as conn:
        now = values.utc_now()
        row = read.required_action(conn, root_id, action_id)
        if (
            (stop is not None and stop.is_set())
            or not fence.lock_claim(conn, claim, now=now)
            or row is None
            or row["source_id"] != claim.source_id
            or row["publication_id"] != publication_id
            or row["request_fingerprint"] != fingerprint
            or row["intent_status"] not in _STARTED
            or read.prepared_request(row) is None
        ):
            return GitHubPublicationOutcome("unknown", "source_fence_or_scope_lost")
        if proof is not None:
            receipt = write.confirm_effect(
                conn,
                source_result_id=root_id,
                action_id=action_id,
                publication_id=publication_id,
                fingerprint=fingerprint,
                proof=proof,
                observed_at=observed_at,
                now=now,
            )
            if receipt is not None:
                source_attention.confirm_publication(
                    conn,
                    claim,
                    root_id=root_id,
                    action_id=action_id,
                    provider="github",
                    publication_id=publication_id,
                    now=now,
                )
                return GitHubPublicationOutcome("published", "remote_effect_confirmed", receipt)
        execution_control.transition_publication_intent(
            conn,
            publication_id,
            PublicationStatus.ATTENTION,
            now,
            error={"reason": "native_github_effect_unconfirmed"},
        )
        source_attention.observe_publication_unknown(
            conn,
            claim,
            root_id=root_id,
            action_id=action_id,
            provider="github",
            publication_id=publication_id,
            reason="native_github_effect_unconfirmed",
            now=now,
        )
        return GitHubPublicationOutcome("unknown", "native_github_effect_unconfirmed")


def _observe_and_settle(db_path, claim, root_id, action_id, publication_id, fingerprint, packet, run, stop):
    try:
        proof = remote.observe_effect(run, packet)
    except ControlDeadlineExceeded:
        raise
    except _REMOTE_ERRORS:
        proof = None
    return _settle(db_path, claim, root_id, action_id, publication_id, fingerprint, proof, values.utc_now(), stop)


def _locked(
    db_path,
    config,
    ticket,
    project,
    row,
    *,
    owner,
    claim,
    observation_provider,
    tapd_client_factory,
    run,
    stop,
    respect_backoff,
    reconcile_only,
    github_discussion_runner,
):
    root_id, action_id = row["root_id"], row["action_id"]
    with _writer(db_path) as conn:
        now = values.utc_now()
        actual = read.required_action(conn, root_id, action_id)
        if actual is None or any(actual[field] != row[field] for field in ("source_id", "task_id", "target_json")):
            return GitHubPublicationOutcome("invalid", "native_action_scope_changed")
        confirmed = read.confirmed_effect(conn, root_id, action_id)
        if confirmed is not None:
            episode = attention_read.publication_episode(conn, confirmed.publication_id)
            held = _claim(conn, ticket, owner, claim, now) if episode and episode["resolved_at"] is None else None
            if held is not None:
                source_attention.confirm_publication(
                    conn,
                    held,
                    root_id=root_id,
                    action_id=action_id,
                    provider="github",
                    publication_id=confirmed.publication_id,
                    now=now,
                )
            return GitHubPublicationOutcome("deduplicated", "remote_effect_already_confirmed", confirmed)
        if actual["publication_id"] is not None and (
            actual["publish_status"] == PublishStatus.PUBLISHED
            or actual["intent_status"] == PublicationStatus.PUBLISHED
        ):
            held = _claim(conn, ticket, owner, claim, now)
            if held is not None:
                source_attention.observe_publication_unknown(
                    conn,
                    held,
                    root_id=root_id,
                    action_id=action_id,
                    provider="github",
                    publication_id=actual["publication_id"],
                    reason="published_receipt_unverified",
                    now=now,
                )
        if actual["publish_status"] != PublishStatus.NOT_PUBLISHED or actual["intent_status"] not in (
            None,
            PublicationStatus.PREPARED,
            *_STARTED,
        ):
            return GitHubPublicationOutcome("deferred", "native_effect_not_retryable")
        if respect_backoff and not schedule.action_is_due(conn, action_id, now):
            return GitHubPublicationOutcome("deferred", "publication_backoff")
        started = actual["intent_status"] in _STARTED
        if not started and reconcile_only:
            return GitHubPublicationOutcome("deferred", "reconciliation_only")
        held = _claim(conn, ticket, owner, claim, now)
        if held is None:
            return GitHubPublicationOutcome("deferred", "source_fence_unavailable")
        predecessor_confirmed = started or _predecessors(conn, actual)
        projected = _projection(conn, actual) if not started and predecessor_confirmed else None
    outcome = (
        _perform(
            db_path,
            config,
            ticket,
            project,
            actual,
            held,
            projected,
            started,
            observation_provider,
            tapd_client_factory,
            run,
            stop,
            github_discussion_runner,
        )
        if predecessor_confirmed
        else GitHubPublicationOutcome("deferred", "required_predecessor_unconfirmed")
    )
    if respect_backoff:
        if outcome.reason == "saved_native_request_unavailable":
            outcome = GitHubPublicationOutcome("invalid", outcome.reason)
        source_publication_queue.pace_action(db_path, held, root_id, action_id, outcome, provider="github", stop=stop)
    return outcome


def _perform(
    db_path,
    config,
    ticket,
    project,
    actual,
    held,
    projected,
    started,
    observation_provider,
    tapd_client_factory,
    run,
    stop,
    github_discussion_runner,
):
    root_id, action_id = actual["root_id"], actual["action_id"]
    if started:
        packet = read.prepared_request(actual)
        publication_id, fingerprint = actual["publication_id"], actual["request_fingerprint"]
        if packet is None:
            return GitHubPublicationOutcome("unknown", "saved_native_request_unavailable")
        return _observe_and_settle(db_path, held, root_id, action_id, publication_id, fingerprint, packet, run, stop)
    if not callable(observation_provider) or projected is None:
        return GitHubPublicationOutcome("deferred", "native_proof_unavailable")
    packet, audit_scope = projected
    with closing(database.connect(db_path)) as conn:
        valid, decision = selection.effect_selection(conn, actual)
        history = selection.creation_history(conn, actual["source_id"], actual["repo_id"]) if decision else ()
        mixed_required = bool(source_members.round_github_request_ids(conn, actual["round_id"]))
    if not valid:
        return GitHubPublicationOutcome("deferred", "native_pr_selection_unavailable")

    def preflight():
        if not native_preparation.recheck_github(audit_scope) or not remote.may_start_effect(run, packet):
            return False
        if decision is None:
            return True
        snapshots = pull_requests.read_native_prs(
            decision["repository"], (entry.number for entry in history or ()), runner=run, stop=stop
        )
        observations = pr_policy.normalize_observations(history, snapshots, decision["publisher"])
        return pr_policy.effect_matches_selection(decision, history, observations)

    try:
        settings = config["tapd"]
        client = tapd_client_factory(executable=settings["executable"], cwd=settings["cwd"], environment=os.environ)
        snapshot = read_ticket_snapshot(client, ticket, project, observation_provider, stop=stop)
    except ControlDeadlineExceeded:
        raise
    except (*_REMOTE_ERRORS, TapdError):
        return GitHubPublicationOutcome("deferred", "native_preflight_unavailable")
    recovery = None
    with _writer(db_path) as conn:
        owner = source_delivery_recovery.active(conn, ticket.source_id)
        recovering = owner is not None and owner["source_result_id"] == root_id
        if recovering:
            if mixed_required:
                return GitHubPublicationOutcome("deferred", "current_source_grant_unavailable")
            now = values.utc_now()
            if not fence.lock_claim(conn, held, now=now):
                return GitHubPublicationOutcome("deferred", "source_fence_unavailable")
            observed = tapd_intake.observe_ticket(
                conn, config["tapd"], snapshot, source_observation.publication_facts(conn, ticket), now=now
            )
            check = source_delivery_resume.effect_check(conn, held, root_id, action_id, revision=observed.revision)
            if check is None:
                source_delivery_resume.record_unproven(
                    conn,
                    config,
                    ticket,
                    held,
                    revision=observed.revision,
                    reason="source_delivery_reprocess_required",
                    now=now,
                    root_id=root_id,
                    action_id=action_id,
                )
    if recovering:
        if check is None:
            return GitHubPublicationOutcome("deferred", "source_delivery_reprocess_required")
        recovery, reason = source_delivery_resume.observe(check, runner=run, stop=stop)
        if recovery is None:
            source_delivery_resume.record_stale(db_path, config, ticket, check, reason)
            return GitHubPublicationOutcome("deferred", reason)
    try:
        if not preflight():
            return GitHubPublicationOutcome("deferred", "native_preflight_refused")
    except ControlDeadlineExceeded:
        raise
    except _REMOTE_ERRORS:
        return GitHubPublicationOutcome("deferred", "native_preflight_unavailable")

    def observe_code_scope(snapshot, boundary):
        return source_delivery_observation.observe(
            db_path,
            config,
            ticket,
            held,
            root_id=root_id,
            action_id=action_id,
            snapshot=snapshot,
            runner=github_discussion_runner,
            stop=stop,
            code_effect=boundary,
        )

    initial = None
    if mixed_required:
        try:
            initial = observe_code_scope(snapshot, source_github_observation.code_prestart(packet))
        except ControlDeadlineExceeded:
            raise
        except (*_REMOTE_ERRORS, errors.Error):
            initial = None
        if initial is None:
            return GitHubPublicationOutcome("deferred", "current_source_grant_unavailable")
    intent = _start(db_path, config, ticket, held, root_id, action_id, snapshot, packet, stop, recovery, initial)
    if intent is None:
        return GitHubPublicationOutcome("deferred", "current_source_grant_unavailable")
    publication_id, fingerprint, revision = intent

    # Revalidate external and local facts before the effect helper's final
    # durable guard. Existing push_target then checks the configured URL again.
    try:
        ready = preflight()
        if ready and recovery is not None:
            refreshed, _ = source_delivery_resume.observe(recovery.check, runner=run, stop=stop)
            ready = refreshed == recovery
    except ControlDeadlineExceeded:
        raise
    except _REMOTE_ERRORS:
        ready = False
    final = None
    if ready and mixed_required:
        try:
            fresh = read_ticket_snapshot(client, ticket, project, observation_provider, stop=stop)
            final = observe_code_scope(fresh, source_github_observation.code_started(publication_id, packet))
        except ControlDeadlineExceeded:
            raise
        except (*_REMOTE_ERRORS, TapdError, errors.Error):
            final = None
        ready = isinstance(final, source_delivery_observation.AcceptedDeliveryObservation)
        if ready:
            revision = final.github.revision
    if not ready:
        return _observe_and_settle(db_path, held, root_id, action_id, publication_id, fingerprint, packet, run, stop)
    native_scope = guards.NativeGitHubWriteScope(
        root_id, publication_id, fingerprint, db_path, config, ticket, held, revision, recovery, final
    )
    action = {**dict(actual), "metadata_json": json.dumps(packet["effect"], ensure_ascii=False)}
    try:
        with closing(database.connect(db_path)) as conn:
            effects.publish_native_github(action, run, conn=conn, native_scope=native_scope)
    except ControlDeadlineExceeded:
        raise
    except _REMOTE_ERRORS:
        pass
    return _observe_and_settle(db_path, held, root_id, action_id, publication_id, fingerprint, packet, run, stop)


def publish_github_action(
    db_path,
    config,
    *,
    source_result_id,
    action_id,
    owner=None,
    claim=None,
    observation_provider=None,
    tapd_client_factory=TapdClient,
    run_command=subprocess.run,
    stop=None,
    respect_backoff=False,
    reconcile_only=False,
    github_discussion_runner=None,
):
    """Handle one confirmed native GitHub action independently of public scheduling."""
    tapd = config.get("tapd") if isinstance(config, Mapping) else None
    if not isinstance(tapd, Mapping) or tapd.get("enabled") is not True:
        return GitHubPublicationOutcome("deferred", "tapd_disabled")
    if (claim is None and (not isinstance(owner, str) or not owner.strip() or len(owner) > 512)) or (
        claim is not None and (not isinstance(claim, fence.SourceClaim) or owner not in (None, claim.owner))
    ):
        return GitHubPublicationOutcome("invalid", "source_owner_required")
    if stop is not None and stop.is_set():
        return GitHubPublicationOutcome("deferred", "stopped")
    with closing(database.connect(db_path)) as conn:
        row = read.required_action(conn, source_result_id, action_id)
        source = source_results.tapd_source(conn, row["source_id"]) if row is not None else None
    if source is None:
        return GitHubPublicationOutcome("invalid", "accepted_native_action_unavailable")
    try:
        ticket = TicketIdentity(source["host"], source["project_key"], source["source_type"], source["external_id"])
        (project,) = (project for project in tapd["projects"] if project["workspace_id"] == ticket.workspace_id)
    except (TypeError, ValueError, KeyError):
        return GitHubPublicationOutcome("invalid", "native_source_scope_unavailable")
    if ticket.source_id != source["source_id"] or tapd.get("host") != ticket.host:
        return GitHubPublicationOutcome("invalid", "native_source_scope_unavailable")
    with source_publication_lock(db_path, ticket.source_id) as locked:
        if not locked:
            return GitHubPublicationOutcome("deferred", "source_publication_locked")
        with task_publication_lock(db_path, row["task_id"]) as locked:
            if not locked:
                return GitHubPublicationOutcome("deferred", "task_publication_locked")
            return _locked(
                db_path,
                config,
                ticket,
                project,
                row,
                owner=owner,
                claim=claim,
                observation_provider=observation_provider,
                tapd_client_factory=tapd_client_factory,
                run=_runner(run_command, stop),
                stop=stop,
                respect_backoff=respect_backoff,
                reconcile_only=reconcile_only,
                github_discussion_runner=github_discussion_runner,
            )
