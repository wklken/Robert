"""Advance one durable TAPD round step; runtime controls cadence and process lanes."""

import json
import subprocess
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import Callable

from robert_agent.adapters.git.source_evidence import EvidenceError, EvidenceReader
from robert_agent.adapters.processes import attempt as process_attempt
from robert_agent.adapters.processes import source_model as model_transport
from robert_agent.adapters.tapd.client import TapdClient
from robert_agent.delivery import native_contract
from robert_agent.delivery.application import (
    result_audit,
    source_delivery_pause,
    source_delivery_resume,
    source_results,
    source_retirement,
    submission,
)
from robert_agent.delivery.lock import task_publication_lock
from robert_agent.execution import spool
from robert_agent.execution.application import dispatch, monitor, source_models
from robert_agent.execution.application.source_models import execute as run_source_stage
from robert_agent.foundation import values
from robert_agent.foundation.contracts.delivery import AuditStatus, OutputType
from robert_agent.foundation.contracts.execution import AttemptStatus, ModelRunStatus, SourceModelStage
from robert_agent.foundation.contracts.work import SourceCapability, SourceRoundStatus, TaskLifecycle
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import database, execution_control, transactions
from robert_agent.persistence.queries import (
    result_contract as task_contract,
)
from robert_agent.persistence.queries import (
    source_coordination as state,
)
from robert_agent.persistence.queries import (
    source_delivery_recovery,
    source_github_ingress,
    source_members,
    source_observations,
    source_tasks,
    sqlite_master,
)
from robert_agent.persistence.queries import (
    source_results as result_queries,
)
from robert_agent.persistence.queries import (
    source_retirement as retirement_queries,
)
from robert_agent.persistence.queries import (
    source_sessions as rounds,
)
from robert_agent.persistence.queries import (
    tapd_intake as intake_queries,
)
from robert_agent.persistence.rounds.source_evidence import json_object
from robert_agent.persistence.stores import (
    agent_runs,
    source_sessions,
)
from robert_agent.persistence.stores import (
    source_coordination as progress,
)
from robert_agent.persistence.stores import (
    tapd_intake as intake_store,
)
from robert_agent.work import source_attention, tapd_authority
from robert_agent.work.application import (
    source_github_observation,
    source_model_observation,
    source_observation,
    source_pr_selection,
    source_preparation,
    source_request_authority,
    tapd_intake,
)
from robert_agent.work.source_pr_selection import ExistingPRTarget

LEASE_SECONDS = 120
_RECHECK_DELAY = {"waiting": 2, "held": 10, "attention": 30, "idle": 30, "publication_pending": 30}
_FINAL_MODEL = {ModelRunStatus.SUCCEEDED, ModelRunStatus.FAILED, ModelRunStatus.INTERRUPTED}
_STATIC_ATTENTION = {
    "unsupported_model_profile",
    "source_stage_failed",
    "source_child_result_invalid",
    "source_stage_missing_evidence",
    "source_preparation_failed",
    "source_route_unavailable",
}


@dataclass(frozen=True, slots=True)
class SourceStep:
    status: str
    source_id: str | None = None
    round_id: str | None = None
    reason: str | None = None
    identity: str | None = None
    attention_repo_id: str | None = None


@dataclass(frozen=True, slots=True)
class _Cycle:
    db_path: Path
    data_dir: Path
    config: dict
    claim: source_sessions.SourceClaim
    ticket: tapd_authority.TicketIdentity
    refresh: Callable | None
    model_runner: Callable
    pr_target: Callable | None
    stop: object
    terminate_worker: Callable
    terminate_model: Callable | None
    github_discussion_runner: Callable | None


@contextmanager
def _writer(db_path):
    with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
        transactions.begin_immediate(conn)
        yield conn


def _step(cycle, status, round_id=None, *, reason=None, identity=None, attention_repo_id=None):
    return SourceStep(status, cycle.ticket.source_id, round_id, reason, identity, attention_repo_id)


def _same_scope(config, ticket):
    settings = config["tapd"]
    return (
        settings["enabled"]
        and settings["host"] == ticket.host
        and any(project["workspace_id"] == ticket.workspace_id for project in settings["projects"])
    )


def _claim(conn, ticket, owner, now):
    saved = rounds.session(conn, ticket.source_id)
    if saved and saved["owner"] == owner:
        claim = source_sessions.SourceClaim(ticket.source_id, owner, saved["token"])
        if source_sessions.renew(conn, claim, now=now, lease_seconds=LEASE_SECONDS):
            return claim
        if source_sessions.lock_claim(conn, claim, now=now):
            return claim
    return source_sessions.claim(conn, source_id=ticket.source_id, owner=owner, now=now, lease_seconds=LEASE_SECONDS)


def _repo(conn, config, repo_id):
    stored = source_tasks.repository(conn, repo_id)
    if stored is None:
        return None
    matches = [
        repo
        for repo in config["repos"]
        if repo["full_name"] == stored["full_name"] and repo.get("enabled", True) is True
    ]
    if len(matches) != 1 or any(
        matches[0].get(key) != stored[key]
        for key in (
            "repo_root",
            "worktree_root",
            "default_base_branch",
            "github_account",
        )
    ):
        return None
    return matches[0]


def _round_repos(conn, cycle, round_, revision, *, mixed_proof=None):
    """Read all current grants and exact configured/persisted repository identities."""
    members = source_sessions.pending_round_members(conn, round_["round_id"])
    if not members:
        return None
    repo_ids = tuple(json.loads(round_["repo_ids_json"]))
    repos = {repo_id: _repo(conn, cycle.config, repo_id) for repo_id in repo_ids}
    if not repos or any(repo is None for repo in repos.values()):
        return None
    names = {repo["full_name"] for repo in repos.values()}
    github = (
        tapd_intake.current_mixed_worker_grants(
            conn,
            cycle.config,
            cycle.ticket,
            cycle.claim,
            round_,
            revision=revision,
            proof=mixed_proof,
            now=values.utc_now(),
        )
        if mixed_proof is not None
        else {}
    )
    if github is None:
        return None
    github_ids = source_members.round_github_request_ids(conn, round_["round_id"])
    for member in members:
        decision = (
            github.get(member["request_id"])
            if member["request_id"] in github_ids
            else tapd_intake.revalidate_request(
                conn, cycle.config["tapd"], cycle.ticket, member["request_id"], revision=revision
            )
        )
        if (
            decision is None
            or decision.reason != "allowed"
            or decision.capability != round_["capability"]
            or not names <= set(decision.repos)
        ):
            return None
    return repos


def _advance(cycle, *, local_only=False):
    with _writer(cycle.db_path) as conn:
        now = values.utc_now()
        if not source_sessions.lock_claim(conn, cycle.claim, now=now):
            return _step(cycle, "held", reason="source_claim_changed")
        active = rounds.active_round(conn, cycle.ticket.source_id)
        cancellation = active and intake_queries.round_cancellation(conn, active["round_id"])
        if (
            active is not None
            and not cancellation
            and not source_members.round_github_request_ids(conn, active["round_id"])
        ):
            last = state.progress(conn, cycle.ticket.source_id)
            if (
                last
                and last["round_id"] == active["round_id"]
                and last["reason"] in _STATIC_ATTENTION
                and tapd_intake.round_input_status(conn, active) in {"current", "frozen"}
            ):
                return _step(cycle, "attention", active["round_id"], reason=last["reason"])
    if local_only:
        return _drain(cycle, active) if cancellation else _step(cycle, "idle")
    if not _same_scope(cycle.config, cycle.ticket):
        return _drain(cycle, active) if cancellation else _step(cycle, "held", reason="source_scope_unavailable")
    assert cycle.refresh is not None
    try:
        observed = cycle.refresh(cycle.ticket)
    except (OSError, ValueError, TimeoutError):
        return _drain(cycle, active) if cancellation else _step(cycle, "held", reason="source_observation_unavailable")
    with _writer(cycle.db_path) as conn:
        now = values.utc_now()
        if (
            observed.claim != cycle.claim
            or observed.observation.facts.identity != cycle.ticket
            or not source_sessions.lock_claim(conn, cycle.claim, now=now)
        ):
            return _step(cycle, "held", reason="source_claim_changed")
        observed = tapd_intake.observe_ticket(
            conn,
            cycle.config["tapd"],
            observed.observation,
            source_observation.publication_facts(conn, cycle.ticket),
            now=now,
        )
        revision = observed.revision
        active = rounds.active_round(conn, cycle.ticket.source_id)
        candidate = _disposition_candidate(conn, cycle.ticket.source_id, active) if revision else None
    if candidate is not None:
        step = _advance_disposition(cycle, candidate, revision)
        if step is not None:
            return step
    return _advance_observed(cycle, observed, revision)


def _disposition_candidate(conn, source_id, active):
    if active and source_members.round_github_request_ids(conn, active["round_id"]):
        root = result_queries.for_round(conn, active["round_id"])
        if root is not None:
            return source_github_observation.accepted_disposition_scope(conn, root["source_result_id"])
    for row in retirement_queries.historical_roots(conn, source_id):
        if source_members.round_github_request_ids(conn, row["round_id"]):
            scope = source_github_observation.accepted_disposition_scope(conn, row["source_result_id"])
            if scope is not None:
                return scope
    return None


def _advance_disposition(cycle, scope, revision):
    disposition = source_github_observation.observe_disposition(
        cycle.db_path,
        cycle.config,
        cycle.claim,
        cycle.ticket,
        scope,
        revision=revision,
        runner=cycle.github_discussion_runner,
        stop=cycle.stop,
    )
    with _writer(cycle.db_path) as conn:
        now = values.utc_now()
        if not source_sessions.lock_claim(conn, cycle.claim, now=now) or not source_github_observation.revision_current(
            conn,
            cycle.ticket,
            revision,
        ):
            return _step(cycle, "held", reason="source_claim_or_observation_changed")
        if scope != source_github_observation.accepted_disposition_scope(conn, scope.root_id):
            return _step(cycle, "held", scope.round_id, reason="accepted_disposition_scope_changed")
        root = result_queries.for_source(conn, cycle.ticket.source_id, scope.root_id)
        round_ = result_queries.delivery_round(conn, cycle.ticket.source_id, scope.round_id)
        if root is None or round_ is None or root["round_id"] != scope.round_id:
            return None
        canceled = intake_queries.round_cancellation(conn, scope.round_id)
        if round_["status"] == SourceRoundStatus.CANCELED and canceled is not None:
            retired = source_retirement.retire_accepted(
                conn,
                cycle.config,
                cycle.ticket,
                cycle.claim,
                round_id=scope.round_id,
                revision=revision,
                now=now,
                disposition=disposition,
            )
            return (
                _step(cycle, "canceled", scope.round_id, reason="accepted_delivery_retired", identity=retired)
                if retired
                else None
            )
        if round_["status"] != SourceRoundStatus.ACTIVE:
            return None
        if canceled is None:
            checked = source_request_authority.current_disposition(
                conn,
                cycle.config,
                cycle.ticket,
                cycle.claim,
                scope.root_id,
                disposition,
                revision=revision,
                now=now,
            )
            reason = checked.revoked[0]["reason"] if checked and checked.revoked else None
            if reason is None and tapd_intake.mixed_same_rule_pause(
                conn,
                cycle.config,
                cycle.ticket,
                root,
                round_,
                revision=revision,
            ):
                reason = "github_development_binding_paused"
            if reason is None:
                return None
            if intake_store.request_cancellation(
                conn,
                source_id=cycle.ticket.source_id,
                round_id=scope.round_id,
                reason=reason,
                now=now,
            ):
                source_request_authority.revoke_unhandled_disposition(conn, cycle.ticket, checked, now=now)
            return _step(cycle, "draining", scope.round_id, reason=reason, identity=scope.root_id)
    return _drain(cycle, round_, revision=revision, disposition=disposition)


def _advance_observed(cycle, observed, revision):
    with _writer(cycle.db_path) as conn:
        now = values.utc_now()
        if not source_sessions.lock_claim(conn, cycle.claim, now=now) or (
            revision is not None and not source_github_observation.revision_current(conn, cycle.ticket, revision)
        ):
            return _step(cycle, "held", reason="source_claim_or_observation_changed")
        active = rounds.active_round(conn, cycle.ticket.source_id)
        if revision is not None:
            for candidate in retirement_queries.historical_roots(conn, cycle.ticket.source_id):
                if source_members.round_github_request_ids(conn, candidate["round_id"]):
                    continue
                retired = source_retirement.retire_accepted(
                    conn,
                    cycle.config,
                    cycle.ticket,
                    cycle.claim,
                    round_id=candidate["round_id"],
                    revision=revision,
                    now=now,
                )
                if retired:
                    return _step(
                        cycle, "canceled", candidate["round_id"], reason="accepted_delivery_retired", identity=retired
                    )
        recovery = None
        mixed = None
        if active is None:
            owner = source_delivery_recovery.active(conn, cycle.ticket.source_id)
            if owner is not None:
                if source_delivery_pause.pause_recovery(
                    conn, cycle.config, cycle.ticket, cycle.claim, owner, revision=revision, now=now
                ):
                    return _step(cycle, "held", reason="accepted_delivery_repaused", identity=owner["source_result_id"])
                recovery, refusal = source_delivery_resume.active_proposal(
                    conn, cycle.config, cycle.ticket, cycle.claim, revision=revision, now=now
                )
                if recovery is None:
                    return _step(
                        cycle,
                        "attention" if refusal else "publication_pending",
                        reason=refusal,
                        identity=owner["source_result_id"],
                    )
            else:
                recovery, refusal = source_delivery_resume.proposal(
                    conn, cycle.config, cycle.ticket, cycle.claim, revision=revision
                )
            if recovery is None:
                source_delivery_resume.record_unproven(
                    conn, cycle.config, cycle.ticket, cycle.claim, revision=revision, reason=refusal, now=now
                )
                if observed.reason == "confirmation_required":
                    resumed = tapd_intake.apply_paused_resume(
                        conn, cycle.config, cycle.ticket, cycle.claim, revision=revision, now=now
                    )
                    if resumed is not None:
                        observed, revision = resumed, resumed.revision
                held_ids = source_github_ingress.pending_held_ids(
                    conn, cycle.ticket.source_id, limit=2 * source_github_observation.BATCH_LIMIT
                )
                idle = _advance_idle(conn, cycle, observed, revision, now, github_candidates=held_ids)
                if refusal is not None and idle.status in ("held", "idle"):
                    return _step(cycle, "held", reason=refusal)
                if (
                    idle.status not in ("held", "idle")
                    or (idle.status == "held" and idle.reason != "waiting_for_confirmation")
                    or idle.reason == "repository_conflict"
                ):
                    return idle
                if not held_ids:
                    return idle
        else:
            round_id = active["round_id"]
            cancellation = intake_queries.round_cancellation(conn, round_id)
            if cancellation:
                # A future delivery policy may admit one fixed pause notice while
                # draining. The notice use case owns that exception and currently refuses it.
                source_results.accept_control_notice(
                    conn, cycle.config, cycle.ticket, cycle.claim, revision=revision, now=now
                )
            root = result_queries.for_round(conn, round_id)
            if not cancellation and root:
                return _step(cycle, "publication_pending", round_id, identity=root["source_result_id"])
            if not cancellation:
                if revision is None or observed.reason in {
                    "context_incomplete",
                    "conflicting_observation",
                    "publication_unknown",
                }:
                    return _step(cycle, "held", round_id, reason="source_observation_unavailable")
                if source_members.round_github_request_ids(conn, round_id):
                    mixed = source_github_observation.active_scope(conn, active)
                    if mixed is None:
                        return _step(cycle, "held", round_id, reason="github_observation_scope_changed")
                else:
                    freshness = tapd_intake.round_input_status(conn, active, revision=revision)
                    if freshness == "changed":
                        replacement = tapd_intake.replace_unlaunched_round(
                            conn,
                            cycle.config,
                            cycle.ticket,
                            cycle.claim,
                            round_id=round_id,
                            replacement_id=values.new_id("source-round"),
                            revision=revision,
                            now=now,
                        )
                        return (
                            _step(cycle, "readmitted", replacement["round_id"])
                            if replacement
                            else _step(cycle, "held", round_id, reason="source_input_retirement_unavailable")
                        )
                    if freshness not in {"current", "frozen"}:
                        return _step(cycle, "held", round_id, reason="source_input_unavailable")
                    repos = _round_repos(conn, cycle, active, revision)
                    if repos is None:
                        return _step(cycle, "held", round_id, reason="source_authority_unavailable")
    if recovery is not None:
        options = {}
        if recovery.discussion is not None:

            def observe_discussion(scope):
                return source_github_observation.observe_recovery(
                    cycle.db_path,
                    cycle.config,
                    cycle.claim,
                    cycle.ticket,
                    scope,
                    revision=revision,
                    runner=cycle.github_discussion_runner,
                    stop=cycle.stop,
                )

            options["observe_discussion"] = observe_discussion
        checked, reason = source_delivery_resume.observe(
            recovery,
            runner=cycle.github_discussion_runner,
            stop=cycle.stop,
            **options,
        )
        if checked is None:
            source_delivery_resume.record_stale(cycle.db_path, cycle.config, cycle.ticket, recovery, reason)
            status = "attention" if reason == "source_delivery_reprocess_required" else "held"
            return _step(cycle, status, reason=reason, identity=recovery.root_id)
        with _writer(cycle.db_path) as conn:
            resumed = (
                source_delivery_resume.confirm_active(
                    conn, cycle.config, cycle.ticket, cycle.claim, checked, revision=revision, now=values.utc_now()
                )
                if recovery.receipt is None
                else source_delivery_resume.apply(
                    conn, cycle.config, cycle.ticket, cycle.claim, checked, revision=revision, now=values.utc_now()
                )
            )
        if resumed:
            return _step(cycle, "publication_pending", identity=recovery.root_id)
        return _step(cycle, "held", reason="source_delivery_recovery_changed")
    if active is None:
        return _refresh_held_github(cycle, revision, held_ids)
    if mixed is not None:
        return _refresh_active_github(cycle, active, mixed, revision)
    if cancellation:
        return _drain(cycle, active, revision=revision)
    if active["capability"] == SourceCapability.DEVELOPMENT:
        return _development(cycle, active, revision, repos)
    return _readonly(cycle, active, revision, repos)


def _advance_idle(conn, cycle, observed, revision, now, *, github_candidates=()):
    source_results.retire_obsolete_control_notices(
        conn, cycle.config, cycle.ticket, cycle.claim, revision=revision, now=now
    )
    if observed.reason == "confirmation_required":
        notice = source_results.accept_confirmation_question(
            conn, cycle.config, cycle.ticket, cycle.claim, revision=revision, now=now
        )
    else:
        notice = source_results.accept_control_notice(
            conn, cycle.config, cycle.ticket, cycle.claim, revision=revision, now=now
        )
    if notice:
        if result_queries.source_control_notice_has_pending_action(conn, notice):
            return _step(cycle, "publication_pending", identity=notice)
        if observed.reason != "confirmation_required":
            return _step(cycle, "idle", reason="repository_conflict")
    waiting = notice is not None and observed.reason == "confirmation_required"
    if rounds.blockers_for_source(conn, cycle.ticket.source_id, limit=1) or (
        waiting and not tapd_intake.prior_results_settled(conn, cycle.ticket)
    ):
        return _step(cycle, "held", reason="prior_source_effect_or_execution")
    if not github_candidates:
        admitted = tapd_intake.admit_round(
            conn,
            cycle.config["tapd"],
            cycle.ticket,
            cycle.claim,
            round_id=values.new_id("source-round"),
            revision=revision,
            now=now,
            configured_repositories=cycle.config["repos"],
        )
        if admitted:
            return _step(cycle, "admitted", admitted["round_id"])
    if waiting:
        return _step(cycle, "held", reason="waiting_for_confirmation")
    return _step(cycle, "idle", reason=observed.reason)


def _refresh_held_github(cycle, revision, request_ids):
    if not revision:
        return _step(cycle, "held", reason="github_observation_tapd_revision_unavailable")
    operations, attempted, failure = source_github_observation.read_operations(
        cycle.db_path,
        cycle.config,
        cycle.claim,
        cycle.ticket,
        revision=revision,
        request_ids=request_ids,
        runner=cycle.github_discussion_runner,
        stop=cycle.stop,
    )
    if failure:
        return _step(cycle, "held", reason=failure, identity=operations[-1][0] if operations else None)
    with _writer(cycle.db_path) as conn:
        if rounds.active_round(conn, cycle.ticket.source_id) is not None:
            return _step(cycle, "held", reason="github_observation_round_changed")
        admitted, observed = tapd_intake.admit_mixed_round(
            conn,
            cycle.config,
            cycle.ticket,
            cycle.claim,
            revision=revision,
            now=values.utc_now(),
            round_id=values.new_id("source-round"),
            operations=operations,
        )
    if not observed:
        return _step(cycle, "held", reason="github_observation_scope_changed")
    if admitted:
        return _step(cycle, "admitted", admitted["round_id"])
    reason = (
        "github_observation_unavailable"
        if any(observation.status == "unavailable" for observation in attempted)
        else "github_request_not_admitted"
    )
    return _step(cycle, "held", reason=reason, identity=operations[-1][0])


def _refresh_active_github(cycle, round_, active, revision):
    operations, _, failure = source_github_observation.read_operations(
        cycle.db_path,
        cycle.config,
        cycle.claim,
        cycle.ticket,
        revision=revision,
        request_ids=active.request_ids,
        runner=cycle.github_discussion_runner,
        stop=cycle.stop,
        active=active,
    )
    if failure:
        return _step(cycle, "held", active.round_id, reason=failure)
    with _writer(cycle.db_path) as conn:
        result = tapd_intake.refresh_mixed_round(
            conn,
            cycle.config,
            cycle.ticket,
            cycle.claim,
            active=active,
            revision=revision,
            operations=operations,
            replacement_id=values.new_id("source-round"),
            now=values.utc_now(),
        )
        proof = result.worker_proof
        repos = _round_repos(conn, cycle, round_, revision, mixed_proof=proof) if proof is not None else None
    if result.status == "cancel_requested":
        return _drain(cycle, round_, revision=revision)
    if result.status != "current":
        return _step(cycle, result.status, result.round_id, reason=result.reason)
    if not repos:
        return _step(cycle, "held", result.round_id, reason="source_authority_unavailable")
    work = _development if round_["capability"] == "development" else _readonly
    return work(cycle, round_, revision, repos, mixed_proof=proof)


def _prepare(cycle, round_, revision, repo_id, role, pr=None, *, selection=None, resolution=None, mixed_proof=None):
    round_id = round_["round_id"]
    try:
        prepared = source_preparation.prepare_source_task(
            cycle.db_path,
            cycle.data_dir,
            cycle.config,
            cycle.claim,
            round_id=round_id,
            revision=revision,
            repo_id=repo_id,
            role=role,
            pr_target=pr,
            pr_selection=selection,
            route_resolution=resolution,
            mixed_proof=mixed_proof,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        prepared = None
    if prepared is None:
        return _step(cycle, "attention", round_id, reason="source_preparation_failed", attention_repo_id=repo_id)
    return _step(cycle, "prepared", round_id, identity=prepared["task_id"])


def _dispatch(cycle, round_id, child, repo, revision=None, *, mixed_proof=None):
    metadata = json_object(child["attempt_metadata_json"]) or {}
    expected = metadata.get("native_pr_selection")
    purpose = metadata.get("native_workspace") or {}
    if expected is not None or purpose.get("pr_selection_required") is True:
        if not isinstance(expected, str) or purpose.get("pr_selection") != expected:
            return _step(cycle, "held", round_id, reason="native_pr_selection_changed", identity=child["attempt_id"])
        selected = source_pr_selection.observe(
            cycle.db_path,
            cycle.config,
            cycle.claim,
            round_id=round_id,
            repo_id=child["repo_id"],
            revision=revision,
            task_id=child["task_id"],
            stop=cycle.stop,
            mixed_proof=mixed_proof,
        )
        if selected.fingerprint != expected:
            return _step(
                cycle,
                "held",
                round_id,
                reason=(selected.reason if selected.status == "held" else "source_pr_prepared_target_changed"),
                identity=child["attempt_id"],
            )
    with _writer(cycle.db_path) as conn:
        if source_sessions.lock_round(conn, cycle.claim, round_id, now=values.utc_now()) is None:
            return _step(cycle, "held", round_id, reason="source_claim_changed")
        target = dispatch.current_dispatch_target(conn, child["attempt_id"])
    if target is None:
        return _step(cycle, "attention", round_id, reason="source_child_result_invalid", identity=child["task_id"])
    queue = []
    dispatch.queue_dispatch(queue, *target, repo_id=child["repo_id"], repo=repo)
    results = dispatch.dispatch_queued(
        cycle.db_path,
        [values.repository_id(row["full_name"]) for row in cycle.config["repos"]],
        queue,
        False,
        cycle.config["max_concurrency"],
        cycle.config["workers"],
        cycle.config["default_worker"],
        cycle.config,
        source_refresh=cycle.refresh,
    )
    if not results:
        return _step(cycle, "waiting", round_id, reason="worker_capacity", identity=child["attempt_id"])
    result = results[0]
    status = result.get("status")
    reason = result.get("reason_code")
    if status in {"running", "completed", "completed_by_worker_result"}:
        return _step(cycle, "dispatched", round_id, identity=child["attempt_id"])
    if reason in {
        "native_input_changed",
        "native_authorization_unavailable",
        "native_source_refresh_unavailable",
        "native_refresh_unavailable",
        "native_frozen_workspace_or_prompt_unavailable",
        "native_pr_selection_changed",
    }:
        return _step(cycle, "held", round_id, reason=reason, identity=child["attempt_id"])
    return _step(cycle, "waiting", round_id, reason="worker_dispatch_not_started", identity=child["attempt_id"])


def _worker_step(cycle, round_id, child, repo, revision=None, *, mixed_proof=None):
    task_id, attempt_id = child["task_id"], child["attempt_id"]
    if not attempt_id:
        return _step(
            cycle, "attention", round_id, reason="source_child_result_invalid", identity=child["task_id"]
        ), None
    with closing(database.connect(cycle.db_path, foreign_keys=True)) as conn:
        candidate_id = result_queries.latest_worker_result_id(conn, task_id)
        candidate = result_queries.worker_candidate(conn, candidate_id) if candidate_id else None
        settled = rounds.attempt_is_settled(
            conn, task_id, attempt_id, subject_type="task", subject_id=task_id, require_exit=True
        )
    if child["attempt_status"] == AttemptStatus.PREPARED:
        with closing(database.connect(cycle.db_path)) as conn:
            binding = execution_control.get_binding(conn, "task", task_id)
        owner = json_object(binding["metadata_json"]) if binding else None
        if owner is not None and owner.get("dispatch_inflight") is None:
            return _dispatch(cycle, round_id, child, repo, revision, mixed_proof=mixed_proof), None
        return _step(cycle, "attention", round_id, reason="worker_launch_unconfirmed", identity=attempt_id), None
    if candidate is not None and settled and child["attempt_status"] == AttemptStatus.COMPLETED:
        metadata = json_object(candidate["metadata_json"]) or {}
        audit = metadata.get("audit")
        if isinstance(audit, dict) and audit.get("status") == AuditStatus.ACCEPTED:
            return None, candidate
        if audit is not None:
            return _step(cycle, "attention", round_id, reason="source_child_result_invalid", identity=task_id), None
        return _audit_worker(cycle, round_id, child, repo), None
    if child["attempt_status"] in {AttemptStatus.RUNNING, AttemptStatus.STALE, AttemptStatus.COMPLETED} and not settled:
        submission.replay_pending_results(cycle.db_path, config=cycle.config, attempt_id=attempt_id)
        with closing(database.connect(cycle.db_path, foreign_keys=True)) as conn, conn:
            monitor.supervise_running_attempts(
                conn,
                child["repo_id"],
                cycle.config,
                values.utc_now(),
                data_dir=cycle.data_dir,
                db_path=cycle.db_path,
                repo=repo,
                resume_attempt=None,
                allow_recovery=False,
                task_id=task_id,
            )
        with closing(database.connect(cycle.db_path)) as conn:
            owner = execution_control.get_binding(conn, "task", task_id)
        if owner and owner["binding_status"] == "attention":
            return _step(cycle, "attention", round_id, reason="worker_identity_unconfirmed", identity=task_id), None
        return _step(cycle, "waiting", round_id, reason="worker_exit_or_result_unconfirmed", identity=task_id), None
    return _step(cycle, "attention", round_id, reason="source_child_result_invalid", identity=task_id), None


def _audit_worker(cycle, round_id, child, repo):
    task_id = child["task_id"]
    with task_publication_lock(cycle.db_path, task_id) as locked:
        if not locked:
            return _step(cycle, "waiting", round_id, reason="worker_effect_lock_busy", identity=task_id)
        run_id = values.new_id("source-audit")
        with _writer(cycle.db_path) as conn:
            now = values.utc_now()
            if source_sessions.lock_round(
                conn, cycle.claim, round_id, now=now
            ) is None or intake_queries.round_cancellation(conn, round_id):
                return _step(cycle, "held", round_id, reason="source_authority_unavailable", identity=task_id)
            agent_runs.insert_for_start_run(conn, run_id, now, None, 0, "{}")
        try:
            with closing(database.connect(cycle.db_path, foreign_keys=True)) as conn, conn:
                count = result_audit.audit_completed_results(
                    conn,
                    child["repo_id"],
                    cycle.data_dir,
                    cycle.db_path,
                    repo,
                    run_id,
                    values.utc_now(),
                    False,
                    [],
                    0,
                    config_result=cycle.config,
                    task_id=task_id,
                )
        except (OSError, ValueError, subprocess.SubprocessError):
            with _writer(cycle.db_path) as conn:
                agent_runs.update_for_interrupt_run(
                    conn, values.utc_now(), '{"reason_code":"source_audit_unavailable"}', run_id
                )
            return _step(cycle, "attention", round_id, reason="source_audit_unavailable", identity=task_id)
        with _writer(cycle.db_path) as conn:
            agent_runs.update_for_finish_run(conn, "completed", values.utc_now(), "{}", run_id)
    return _step(cycle, "audited" if count else "waiting", round_id, identity=task_id)


def _accept_worker(cycle, round_id, revision, candidate, *, mixed_proof=None):
    with _writer(cycle.db_path) as conn:
        result_id = source_results.accept_worker_result(
            conn,
            cycle.config,
            cycle.ticket,
            cycle.claim,
            round_id=round_id,
            worker_result_id=candidate["result_id"],
            revision=revision,
            now=values.utc_now(),
            mixed_proof=mixed_proof,
        )
    return (
        _step(cycle, "publication_pending", round_id, identity=result_id)
        if result_id
        else _step(cycle, "held", round_id, reason="source_root_not_accepted")
    )


def _fixture_pr(cycle, round_id, repo_id):
    with closing(database.connect(cycle.db_path)) as conn:
        lineage = state.has_pr_lineage(conn, cycle.ticket.source_id)
    if not lineage:
        return False, None
    if cycle.pr_target is None:
        return True, None
    try:
        target = cycle.pr_target(cycle.ticket, round_id=round_id, repo_id=repo_id)
    except (OSError, ValueError, subprocess.SubprocessError):
        return True, None
    return True, target if isinstance(target, ExistingPRTarget) else None


def _development(cycle, round_, revision, repos, *, mixed_proof=None):
    round_id = round_["round_id"]
    if len(repos) != 1:
        return _step(cycle, "attention", round_id, reason="source_route_unavailable")
    repo_id, repo = next(iter(repos.items()))
    with closing(database.connect(cycle.db_path)) as conn:
        children = state.tasks(conn, round_id)
    if not children:
        _, pr = _fixture_pr(cycle, round_id, repo_id) if cycle.pr_target is not None else (False, None)
        return _prepare(cycle, round_, revision, repo_id, "classification", pr, mixed_proof=mixed_proof)
    if (
        len(children) > 2
        or children[0]["route_id"] != "classification-result"
        or any(child["repo_id"] != repo_id for child in children)
    ):
        return _step(cycle, "attention", round_id, reason="source_child_result_invalid")
    child = children[-1]
    step, candidate = _worker_step(cycle, round_id, child, repo, revision, mixed_proof=mixed_proof)
    if step:
        return step
    if len(children) == 2:
        if child["route_id"] not in {"comment-analysis", "waiting-for-user", "new-pr", "update-existing-pr"}:
            return _step(cycle, "attention", round_id, reason="source_child_result_invalid")
        return _accept_worker(cycle, round_id, revision, candidate, mixed_proof=mixed_proof)
    if candidate["output_type"] != OutputType.CLASSIFICATION_RESULT or child["lifecycle"] != TaskLifecycle.COMPLETED:
        return _step(cycle, "attention", round_id, reason="source_child_result_invalid")
    metadata = json_object(candidate["metadata_json"]) or {}
    route = metadata.get("recommended_route")
    with closing(database.connect(cycle.db_path)) as conn:
        classifier_binding = task_contract.task_round_binding(conn, candidate["task_id"])
    if classifier_binding is None:
        return _step(cycle, "attention", round_id, reason="source_child_result_invalid")
    classified_with_pr = classifier_binding["primary_provider"] == "github"
    if route not in native_contract.classification_routes(SourceCapability.DEVELOPMENT, existing_pr=classified_with_pr):
        return _step(cycle, "attention", round_id, reason="source_route_unavailable")
    role = {"comment-analysis": "answer", "waiting-for-user": "question"}.get(route, "development")
    if role != "development":
        return _prepare(cycle, round_, revision, repo_id, role, mixed_proof=mixed_proof)
    if cycle.pr_target is not None:
        required, pr = _fixture_pr(cycle, round_id, repo_id)
        if required and pr is None:
            return _step(cycle, "held", round_id, reason="source_pr_target_unresolved")
        return _prepare(cycle, round_, revision, repo_id, role, pr, mixed_proof=mixed_proof)
    selected = source_pr_selection.observe(
        cycle.db_path,
        cycle.config,
        cycle.claim,
        round_id=round_id,
        repo_id=repo_id,
        revision=revision,
        stop=cycle.stop,
        mixed_proof=mixed_proof,
    )
    if selected.status == "held":
        return _step(cycle, "held", round_id, reason=selected.reason, identity=selected.fingerprint)
    route_resolution = {
        "classification_result_id": candidate["result_id"],
        "recommended_route": route,
        "selected_route": "update-existing-pr" if selected.target else "new-pr",
        "reason": selected.reason,
        "pr_selection": selected.fingerprint,
    }
    return _prepare(
        cycle,
        round_,
        revision,
        repo_id,
        role,
        selected.target,
        selection=selected.fingerprint,
        resolution=route_resolution,
        mixed_proof=mixed_proof,
    )


def _model_id(round_id, stage):
    return "source-model:" + sha256(f"{round_id}:{stage}".encode()).hexdigest()


def _accept_model(cycle, round_id, revision, run_id, *, mixed_proof=None):
    with _writer(cycle.db_path) as conn:
        root = source_results.accept_model_result(
            conn,
            cycle.config,
            cycle.ticket,
            cycle.claim,
            round_id=round_id,
            model_run_id=run_id,
            revision=revision,
            now=values.utc_now(),
            mixed_proof=mixed_proof,
        )
    return (
        _step(cycle, "publication_pending", round_id, identity=root)
        if root
        else _step(cycle, "attention", round_id, reason="source_stage_failed", identity=run_id)
    )


def _run_model(cycle, round_, revision, stage, repos, *, children=(), mixed_proof=None):
    round_id = round_["round_id"]
    run_id = _model_id(round_id, stage)
    with closing(database.connect(cycle.db_path)) as conn:
        run = rounds.model_run(conn, run_id)
        mixed = bool(source_members.round_github_request_ids(conn, round_id))
    if run is not None and run["status"] in _FINAL_MODEL:
        return (
            _accept_model(cycle, round_id, revision, run_id, mixed_proof=mixed_proof)
            if run["status"] == ModelRunStatus.SUCCEEDED
            else (_step(cycle, "attention", round_id, reason="source_stage_failed", identity=run_id))
        )
    if run is not None and run["launch_id"] is not None:
        recovered = source_models.recover(cycle.db_path, cycle.claim, run_id=run_id)
        return _step(cycle, "attention", round_id, reason=recovered.reason, identity=run_id)
    try:
        profile = source_models.select_worker(cycle.config)
    except (ValueError, model_transport.UnsupportedProfile):
        return _step(cycle, "attention", round_id, reason="unsupported_model_profile", identity=run_id if run else None)
    evidence, omissions = (), ()
    if run is None:
        try:
            with EvidenceReader({repo_id: repo["repo_root"] for repo_id, repo in repos.items()}) as reader:
                if stage == SourceModelStage.IDENTIFY:
                    batch = reader.initial()
                    evidence, omissions = batch.records, batch.omissions
                else:
                    refs = sorted(
                        {
                            (ref["repo_id"], ref["path"], ref["revision"])
                            for child in children
                            for ref in child["content"]["references"]
                        }
                    )
                    evidence = tuple(reader.read(repo_id, path) for repo_id, path, _ in refs)
                    if any(record.revision != ref[2] for record, ref in zip(evidence, refs, strict=True)):
                        raise EvidenceError("source_evidence_changed")
        except (EvidenceError, OSError, ValueError):
            return _step(cycle, "attention", round_id, reason="source_stage_missing_evidence")
    with closing(database.connect(cycle.db_path)) as conn:
        members = rounds.round_requests(conn, round_id)
    if not members:
        return _step(cycle, "held", round_id, reason="source_authority_unavailable", identity=run_id)

    def authorize(conn, claim, checked_round, request_id, checked_stage):
        if claim != cycle.claim or checked_round != round_id or checked_stage != stage:
            return False
        current = rounds.active_round(conn, cycle.ticket.source_id)
        # A background observation may update ordinary context; validate all
        # current grants against its revision while frozen inputs stay unchanged.
        row = source_observations.source(conn, cycle.ticket.source_id)
        source_metadata = json_object(row["metadata_json"]) if row else None
        return bool(
            current
            and current["round_id"] == round_id
            and source_metadata
            and source_metadata.get("revision")
            and _round_repos(conn, cycle, current, source_metadata["revision"])
            and not source_observation.publication_facts(conn, cycle.ticket).unknown_intents
            and not result_queries.unknown_github_effect(conn, cycle.ticket.source_id)
        )

    result = run_source_stage(
        cycle.db_path,
        cycle.claim,
        round_id=round_id,
        request_id=members[0]["request_id"],
        run_id=run_id,
        stage=stage,
        profile=profile,
        evidence=evidence,
        omissions=omissions,
        child_results=children,
        authorize=authorize,
        mixed_observer=(
            source_model_observation.MixedModelObserver(
                cycle.db_path, cycle.config, cycle.ticket, cycle.claim, cycle.refresh, cycle.github_discussion_runner
            )
            if mixed
            else None
        ),
        stop=cycle.stop,
        runner=cycle.model_runner,
    )
    if result.status == ModelRunStatus.SUCCEEDED:
        return _step(cycle, "stage_recorded", round_id, identity=run_id)
    if result.status == "waiting":
        return _step(cycle, "waiting", round_id, reason=result.reason, identity=run_id)
    if result.status in {"denied", "interrupted", "existing"}:
        with closing(database.connect(cycle.db_path)) as conn:
            canceled = intake_queries.round_cancellation(conn, round_id) is not None
        return _step(cycle, "draining" if canceled else "held", round_id, reason=result.reason, identity=run_id)
    if result.reason == "unsupported_source_model_profile":
        return _step(cycle, "attention", round_id, reason=result.reason, identity=run_id if run else None)
    if result.reason in {"invalid_source_model_input", "required_source_context_exceeds_capacity"}:
        return _step(cycle, "attention", round_id, reason="source_stage_failed", identity=run_id if run else None)
    if result.status == "failed":
        return _step(cycle, "attention", round_id, reason="source_stage_failed", identity=run_id)
    return _step(cycle, "attention", round_id, reason=result.reason, identity=run_id)


def _readonly(cycle, round_, revision, repos, *, mixed_proof=None):
    round_id = round_["round_id"]
    with closing(database.connect(cycle.db_path)) as conn:
        identify_id = _model_id(round_id, SourceModelStage.IDENTIFY)
        identified = rounds.model_run(conn, identify_id)
        verified = source_models.verified_model_output(conn, round_, identify_id) if identified else None
        children = state.tasks(conn, round_id)
        existing_models = rounds.model_runs(conn, round_id) if mixed_proof is not None else ()
    if any(
        run["stage"] not in (SourceModelStage.IDENTIFY, SourceModelStage.SYNTHESIS)
        or run["run_id"] != _model_id(round_id, run["stage"])
        for run in existing_models
    ):
        return _step(cycle, "held", round_id, reason="github_mixed_model_unavailable")
    if len(repos) > 1 or identified is not None:
        if verified is None:
            return _run_model(cycle, round_, revision, SourceModelStage.IDENTIFY, repos, mixed_proof=mixed_proof)
        output = verified[2]
        if output["kind"] == "question":
            return _accept_model(cycle, round_id, revision, identify_id, mixed_proof=mixed_proof)
        selected = tuple(output["repositories"])
    else:
        selected = tuple(repos)
    if len({row["repo_id"] for row in children}) != len(children) or any(
        row["route_id"] != "source-evidence" or row["repo_id"] not in selected for row in children
    ):
        return _step(cycle, "attention", round_id, reason="source_child_result_invalid")
    for child in children:
        step, candidate = _worker_step(
            cycle, round_id, child, repos[child["repo_id"]], revision, mixed_proof=mixed_proof
        )
        if step:
            return step
        if candidate["output_type"] != OutputType.LOCAL_RESULT:
            return _step(cycle, "attention", round_id, reason="source_child_result_invalid")
    completed_repos = {row["repo_id"] for row in children}
    for repo_id in selected:
        if repo_id not in completed_repos:
            return _prepare(cycle, round_, revision, repo_id, "evidence", mixed_proof=mixed_proof)
    with closing(database.connect(cycle.db_path)) as conn:
        evidence_children = source_models.synthesis_children(conn, round_)
    if not evidence_children:
        return _step(cycle, "attention", round_id, reason="source_child_result_invalid")
    return _run_model(
        cycle, round_, revision, SourceModelStage.SYNTHESIS, repos, children=evidence_children, mixed_proof=mixed_proof
    )


def _cancel_worker(cycle, round_id, task_id):
    with task_publication_lock(cycle.db_path, task_id) as locked:
        if not locked:
            return "worker_effect_lock_busy"
        with _writer(cycle.db_path) as conn:
            if (
                source_sessions.lock_round(conn, cycle.claim, round_id, now=values.utc_now()) is None
                or intake_queries.round_cancellation(conn, round_id) is None
            ):
                return "source_claim_changed"
            binding = execution_control.get_binding(conn, "task", task_id)
            metadata = json_object(binding["metadata_json"]) if binding else None
            if metadata is None or metadata.get("dispatch_inflight") is not None:
                return "worker_launch_unconfirmed"
            owned_attempts = [row for row in rounds.worker_attempts(conn, round_id) if row["task_id"] == task_id]
            attempt_ids = sorted(row["attempt_id"] for row in owned_attempts)
            attempts = [
                row
                for row in owned_attempts
                if not rounds.attempt_is_settled(
                    conn, task_id, row["attempt_id"], subject_type="task", subject_id=task_id
                )
            ]
        updates = []
        for row in attempts:
            metadata = json_object(row["metadata_json"])
            if metadata is None or not isinstance(metadata.get("dispatch"), dict):
                return "worker_identity_unconfirmed"
            try:
                outcome = cycle.terminate_worker(metadata)
            except (OSError, ValueError, subprocess.SubprocessError):
                return "worker_exit_unconfirmed"
            if outcome.get("status") not in {"terminated", "already_dead", "not_found"}:
                return "worker_exit_unconfirmed"
            updates.append((row, outcome))
        try:
            with ExitStack() as locks:
                for attempt_id in attempt_ids:
                    locks.enter_context(spool.locked_record(cycle.db_path, attempt_id, blocking=False))
                with _writer(cycle.db_path) as conn:
                    if not progress.settle_canceled_worker(
                        conn, cycle.claim, round_id, task_id, now=values.utc_now(), attempt_updates=updates
                    ):
                        return "worker_cancellation_changed"
        except BlockingIOError:
            return "worker_result_slot_busy"
    return None


def _drain(cycle, round_, *, revision=None, disposition=None):
    round_id = round_["round_id"]
    with closing(database.connect(cycle.db_path)) as conn:
        models = rounds.model_runs(conn, round_id)
        children = state.tasks(conn, round_id)
        root = result_queries.for_round(conn, round_id)
        blocked = rounds.blockers_for_source(conn, cycle.ticket.source_id, limit=100)
        attempts = rounds.worker_attempts(conn, round_id)
    for row in models:
        if row["status"] not in _FINAL_MODEL:
            outcome = source_models.cancel_recorded(
                cycle.db_path,
                cycle.claim,
                run_id=row["run_id"],
                terminate=cycle.terminate_model,
            )
            return _step(
                cycle,
                "draining" if outcome.status == "interrupted" else "attention",
                round_id,
                reason=outcome.reason,
                identity=row["run_id"],
            )
    process_blockers = {item.identity for item in blocked if item.kind in {"task", "launch"}}
    attempt_blockers = {item.identity for item in blocked if item.kind == "attempt"}
    unproved_tasks = {row["task_id"] for row in attempts if row["attempt_id"] in attempt_blockers}
    for child in children:
        if child["lifecycle"] in {TaskLifecycle.QUEUED, TaskLifecycle.RUNNING, TaskLifecycle.WAITING_FOR_USER} or (
            child["task_id"] in process_blockers or child["task_id"] in unproved_tasks
        ):
            reason = _cancel_worker(cycle, round_id, child["task_id"])
            return _step(
                cycle, "attention" if reason else "draining", round_id, reason=reason, identity=child["task_id"]
            )
    if root:
        with _writer(cycle.db_path) as conn:
            now = values.utc_now()
            retired = source_retirement.retire_accepted(
                conn,
                cycle.config,
                cycle.ticket,
                cycle.claim,
                round_id=round_id,
                revision=revision,
                now=now,
                disposition=disposition,
            )
            if retired:
                return _step(cycle, "canceled", round_id, reason="accepted_delivery_retired", identity=retired)
            dormant = source_delivery_pause.pause_delivery(
                conn,
                cycle.config,
                cycle.ticket,
                cycle.claim,
                round_id=round_id,
                revision=revision,
                now=values.utc_now(),
                disposition=disposition,
            )
        if dormant:
            return _step(cycle, "canceled", round_id, reason="accepted_delivery_paused", identity=dormant)
        return _step(
            cycle, "held", round_id, reason="source_publication_settlement_required", identity=root["source_result_id"]
        )
    with _writer(cycle.db_path) as conn:
        canceled = source_sessions.finish_round(
            conn,
            cycle.claim,
            round_id=round_id,
            outcome=SourceRoundStatus.CANCELED,
            now=values.utc_now(),
        )
    return (
        _step(cycle, "canceled", round_id)
        if canceled
        else _step(cycle, "attention", round_id, reason="source_cancellation_unsettled")
    )


def _configured_scopes(config):
    tapd = config.get("tapd", {})
    return [(tapd["host"], project["workspace_id"]) for project in tapd["projects"]] if tapd.get("enabled") else []


def has_due_source(db_path, config, *, owner, local_only=False):
    """Probe paced work without I/O to the provider; owner=None is diagnostic."""
    if not Path(db_path).is_file():
        return False
    with closing(database.connect(db_path)) as conn:
        return sqlite_master.table_exists(conn, "tapd_coordination") and (
            state.candidate(conn, values.utc_now(), owner, _configured_scopes(config), local_only=local_only)
            is not None
        )


def advance_source(
    db_path,
    data_dir,
    config,
    *,
    owner,
    ticket=None,
    client_factory=TapdClient,
    observation_provider=None,
    model_runner=model_transport.run,
    pr_target=None,
    github_discussion_runner=None,
    stop=None,
    terminate_worker=process_attempt.terminate_attempt,
    terminate_model=None,
    local_only=False,
):
    """Advance a known source once using one stable lane owner across repeated calls.

    Selects persisted oldest eligible work when ticket is omitted. A source
    claim is renewed before external refresh, and accepted model launch keeps
    its own cooperative lease checks. This entrypoint never performs publication.
    """
    if not owner:
        raise ValueError("source coordinator owner is required")
    if stop is not None and stop.is_set():
        return SourceStep("stopped")
    with _writer(db_path) as conn:
        now = values.utc_now()
        row = (
            state.candidate(conn, now, owner, _configured_scopes(config), local_only=local_only)
            if ticket is None
            else source_observations.source(conn, ticket.source_id)
        )
        if row is None:
            return SourceStep("idle")
        ticket = tapd_authority.TicketIdentity(row["host"], row["project_key"], row["source_type"], row["external_id"])
        if ticket.source_id != row["source_id"]:
            return SourceStep("attention", row["source_id"], reason="source_identity_inconsistent")
        active = rounds.active_round(conn, ticket.source_id)
        if local_only and (active is None or not intake_queries.round_cancellation(conn, active["round_id"])):
            return SourceStep("idle")
        scope_removed = None if local_only else source_observation.removed_scope(config, ticket, row)
        if (
            not local_only
            and active
            and not scope_removed
            and any(_repo(conn, config, repo_id) is None for repo_id in json.loads(active["repo_ids_json"]))
        ):
            scope_removed = "source_repository_removed"
        if active is None and not _same_scope(config, ticket):
            return SourceStep("disabled", ticket.source_id)
        claim = _claim(conn, ticket, owner, now)
        if claim is None:
            return SourceStep("held", ticket.source_id, reason="source_lease_busy")
        if active and scope_removed:
            intake_store.request_cancellation(
                conn,
                source_id=ticket.source_id,
                round_id=active["round_id"],
                reason=scope_removed,
                now=now,
            )
    refresh = (
        None
        if local_only
        else source_observation.make_source_refresh(
            db_path,
            config,
            owner=owner,
            claim=claim,
            client_factory=client_factory,
            observation_provider=observation_provider,
            github_discussion_runner=github_discussion_runner,
            stop=stop,
            lease_seconds=LEASE_SECONDS,
        )
    )
    cycle = _Cycle(
        Path(db_path),
        Path(data_dir),
        config,
        claim,
        ticket,
        refresh,
        model_runner,
        pr_target,
        stop,
        terminate_worker,
        terminate_model,
        github_discussion_runner,
    )
    with task_publication_lock(db_path, "source-coordinator:" + ticket.source_id) as locked:
        if not locked:
            return SourceStep("held", ticket.source_id, reason="source_coordinator_busy")
        result = _advance(cycle, local_only=local_only)
        _save_progress(db_path, claim, result)
    return result


def _save_progress(db_path, claim, result):
    if result.source_id != claim.source_id:
        return
    with _writer(db_path) as conn:
        now = values.utc_now()
        next_due = utc_timestamp(now) + timedelta(seconds=_RECHECK_DELAY.get(result.status, 0))
        source_attention.save_coordination_progress(
            conn,
            claim,
            round_id=result.round_id,
            status=result.status,
            reason=result.reason,
            identity=result.identity,
            repo_id=result.attention_repo_id,
            next_due_at=next_due.isoformat(),
            now=now,
        )
