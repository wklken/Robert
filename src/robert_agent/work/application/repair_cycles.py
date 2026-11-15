"""Observe and charge one TAPD repair cycle under current source ownership."""

from dataclasses import asdict, dataclass
import json
from math import isfinite
import re
from uuid import uuid4

from robert_agent.foundation.contracts.work import RemediationStatus, SourceCapability
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence.queries import repair_cycles as read
from robert_agent.persistence.stores import repair_cycles as write
from robert_agent.persistence.stores import source_sessions
from robert_agent.work import repair_budget
from robert_agent.work.tapd_authority import digest
from robert_agent.work.application.tapd_intake import current_label_authority, revalidate_request


@dataclass(frozen=True, slots=True)
class CheckObservation:
    name: str
    head_sha: str
    status: str
    conclusion: str | None


@dataclass(frozen=True, slots=True)
class PullObservation:
    pr_source_id: str
    host: str
    repo: str
    number: int
    head_sha: str
    base_sha: str
    base_ref: str
    state: str
    merged: bool | None
    mergeable: bool | None
    observed_at: str
    checks: tuple[CheckObservation, ...] = ()
    checks_complete: bool = False


@dataclass(frozen=True, slots=True)
class RepairDecision:
    status: str
    cycle_id: str | None = None


def _linked_scope(conn, source_id, observation):
    scope = read.native_pr_scope(conn, source_id=source_id, pr_source_id=observation.pr_source_id)
    if (
        scope is None
        or type(observation.number) is not int
        or observation.host != "github.com"
        or observation.number != scope.number
        or not isinstance(observation.repo, str)
        or observation.repo.casefold() != scope.repository.casefold()
        or any(
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None
            for value in (observation.head_sha, observation.base_sha)
        )
        or not isinstance(observation.base_ref, str)
        or not observation.base_ref.strip()
    ):
        return None
    return scope


def _configured_project(tapd_config, ticket):
    if (
        not isinstance(tapd_config, dict)
        or tapd_config.get("enabled") is not True
        or tapd_config.get("host") != ticket.host
    ):
        return None
    projects = [
        project for project in tapd_config.get("projects", ()) if project["workspace_id"] == ticket.workspace_id
    ]
    return projects[0] if len(projects) == 1 else None


def _policy(project, current, scope):
    if (
        current.reason != "allowed"
        or current.capability != SourceCapability.DEVELOPMENT
        or scope.repository not in current.repos
        or scope.repository not in project["repositories"]
        or not current.rule
    ):
        return None
    rule = next((rule for rule in project["rules"] if all(rule.get(k) == v for k, v in current.rule.items())), None)
    return rule["repair"] if rule is not None else None


def _authorized(conn, tapd_config, ticket, claim, *, round_id, request_id, revision, observation, now):
    project = _configured_project(tapd_config, ticket)
    if project is None or not conn.in_transaction or ticket.source_id != claim.source_id:
        return None
    active = source_sessions.lock_round(conn, claim, round_id, now=now)
    if (
        active is None
        or active["capability"] != SourceCapability.DEVELOPMENT
        or not source_sessions.pending_member(conn, round_id, request_id)
    ):
        return None
    scope = _linked_scope(conn, claim.source_id, observation)
    if scope is None:
        return None
    try:
        repos = json.loads(active["repo_ids_json"])
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(repos, list) or scope.repo_id not in repos:
        return None
    current = revalidate_request(conn, tapd_config, ticket, request_id, revision=revision)
    policy = _policy(project, current, scope)
    return (scope, policy) if policy is not None else None


def _observation_time(observation, cycle, now, *, creation_observed_at):
    current, observed = utc_timestamp(now), utc_timestamp(observation.observed_at)
    creation = utc_timestamp(creation_observed_at)
    saved_at = cycle["observed_at"] if cycle else None
    previous = utc_timestamp(saved_at)
    if (
        None in (creation, observed, current)
        or not creation <= observed <= current
        or (saved_at is not None and previous is None)
    ):
        return None
    if previous is not None and (
        observed < previous or (observed == previous and cycle["observation_digest"] != digest(asdict(observation)))
    ):
        return None
    return observed


def _observe(conn, cycle, observation, required_checks, now, policy, *, original_base):
    status = repair_budget.health_status(
        observation, authorized_base_ref=original_base, required_checks=required_checks
    )
    if cycle["authorized_base_ref"] != original_base and status != "remote_pr_terminal":
        status = "base_ref_changed"
    state, reason = cycle["status"], cycle["close_reason"]
    if status == "remote_pr_terminal" or (
        status == "health_recovered"
        and state == "active"
        and not read.source_repair_unsettled(conn, cycle["source_id"])
    ):
        state, reason = "closed", status
    elif status == "base_ref_changed":
        state, reason = "paused", status
    elif status == "health_recovered":
        status = "health_waits_for_work"
    if status == "repair_pending" and not (policy["ci"]["enabled"] or policy["conflict"]["enabled"]):
        status = "policy_disabled"
    write.observe_native_cycle(
        conn,
        cycle_id=cycle["cycle_id"],
        status=state,
        reason=reason,
        observed_at=observation.observed_at,
        observation_digest=digest(asdict(observation)),
        now=now,
    )
    return "paused" if state == "paused" and status != "base_ref_changed" else status


def _required_checks(saved, current):
    return set(saved["ci"]["check_allowlist"]) | set(current["ci"]["check_allowlist"])


def apply_observation(conn, tapd_config, ticket, claim, *, revision, observation, now):
    """Track the linked PR under current label authority without creating a request."""
    project = _configured_project(tapd_config, ticket)
    if (
        project is None
        or not conn.in_transaction
        or ticket.source_id != claim.source_id
        or not source_sessions.lock_claim(conn, claim, now=now)
    ):
        return RepairDecision("authority_unavailable")
    scope = _linked_scope(conn, claim.source_id, observation)
    if scope is None:
        return RepairDecision("authority_unavailable")
    current = current_label_authority(conn, tapd_config, ticket, revision=revision)
    policy = _policy(project, current, scope)
    cycle = read.current_native_cycle(conn, claim.source_id, observation.pr_source_id)
    if cycle is None:
        if policy is None:
            return RepairDecision("authority_unavailable")
        if _observation_time(observation, None, now, creation_observed_at=scope.observed_at) is None:
            return RepairDecision("stale_health")
        if observation.state == "open" and observation.merged is False and observation.base_ref != scope.base_branch:
            return RepairDecision("base_ref_changed")
        return RepairDecision("no_cycle")
    if _observation_time(observation, cycle, now, creation_observed_at=scope.observed_at) is None:
        return RepairDecision("stale_health", cycle["cycle_id"])
    if policy is None and observation.state != "closed" and observation.merged is not True:
        return RepairDecision("authority_unavailable", cycle["cycle_id"])
    saved = json.loads(cycle["policy_json"])
    tightened = _effective_policy(conn, cycle, saved, policy, now) if policy is not None else saved
    reason = _observe(
        conn,
        cycle,
        observation,
        _required_checks(saved, policy or saved),
        now,
        tightened,
        original_base=scope.base_branch,
    )
    return RepairDecision(reason, cycle["cycle_id"])


def _effective_policy(conn, cycle, saved, current, now):
    cap = json.loads(cycle["policy_cap_json"]) if cycle and cycle["policy_cap_json"] else saved
    effective = repair_budget.effective_native_policy(cap, current)
    if cycle and effective != cap:
        write.tighten_native_policy(
            conn,
            cycle_id=cycle["cycle_id"],
            policy_cap_json=json.dumps(effective, sort_keys=True, allow_nan=False),
            now=now,
        )
    return effective


def claim_repair(
    conn, tapd_config, ticket, claim, *, round_id, request_id, revision, episode_id, task_id, observation, now
):
    """Charge one bound logical request. This ledger does not grant effects."""
    authorized = _authorized(
        conn,
        tapd_config,
        ticket,
        claim,
        round_id=round_id,
        request_id=request_id,
        revision=revision,
        observation=observation,
        now=now,
    )
    if authorized is None:
        return RepairDecision("authority_unavailable")
    scope, policy = authorized
    episode = read.native_episode_for_task(
        conn,
        round_id=round_id,
        request_id=request_id,
        task_id=task_id,
        episode_id=episode_id,
        workstream_id=scope.workstream_id,
        repo_id=scope.repo_id,
    )
    if (
        episode is None
        or episode["status"] not in (RemediationStatus.OPEN, RemediationStatus.REMEDIATING)
        or (episode["observed_head_sha"], episode["observed_base_sha"]) != (observation.head_sha, observation.base_sha)
    ):
        return RepairDecision("episode_unavailable")
    cycle = read.current_native_cycle(conn, claim.source_id, observation.pr_source_id)
    saved = json.loads(cycle["policy_json"]) if cycle else policy
    mode = "ci" if episode["episode_kind"] == "ci" else "conflict"
    existing = read.claim_by_request(conn, request_id)
    if existing and (
        cycle is None
        or existing["cycle_id"] != cycle["cycle_id"]
        or existing["task_id"] != task_id
        or existing["episode_id"] != episode_id
        or existing["round_id"] != round_id
    ):
        return RepairDecision("request_already_bound")
    prior = cycle or read.latest_native_cycle(conn, claim.source_id, observation.pr_source_id)
    timestamp = _observation_time(observation, prior, now, creation_observed_at=scope.observed_at)
    previous = utc_timestamp(prior["observed_at"]) if prior else None
    if timestamp is None or (cycle is None and prior and (previous is None or timestamp <= previous)):
        return RepairDecision("stale_health", cycle["cycle_id"] if cycle else None)
    effective = _effective_policy(conn, cycle, saved, policy, now)
    limits = effective[mode]
    if cycle:
        reason = _observe(
            conn, cycle, observation, _required_checks(saved, policy), now, effective, original_base=scope.base_branch
        )
        if reason != "repair_pending":
            return RepairDecision(reason, cycle["cycle_id"])
    elif prior and prior["close_reason"] != "health_recovered":
        return RepairDecision("renewal_unavailable", prior["cycle_id"])
    elif prior and prior["authorized_base_ref"] != scope.base_branch:
        return RepairDecision("base_ref_changed", prior["cycle_id"])
    else:
        reason = repair_budget.health_status(
            observation, authorized_base_ref=scope.base_branch, required_checks=set(policy["ci"]["check_allowlist"])
        )
        if reason != "repair_pending":
            return RepairDecision(
                reason if reason == "base_ref_changed" else "repair_not_required",
                prior["cycle_id"] if prior and reason == "base_ref_changed" else None,
            )
    if not limits["enabled"] or (mode == "ci" and not limits["check_allowlist"]):
        return RepairDecision("policy_disabled", cycle["cycle_id"] if cycle else None)
    if not repair_budget.repair_signal(observation, mode=mode, allowed_checks=limits.get("check_allowlist", ())):
        return RepairDecision("repair_signal_unavailable", cycle["cycle_id"] if cycle else None)
    cycle_id = cycle["cycle_id"] if cycle else None
    claims = read.cycle_claims(conn, cycle_id) if cycle else []
    measurements = read.cycle_attempts(conn, cycle_id) if cycle else []
    budget = repair_budget.status(
        attempt_count=len(claims),
        started_at=cycle["started_at"] if cycle else None,
        policy={**limits, "max_attempts": effective["max_attempts"]},
        usage=repair_budget.native_usage(measurements),
        now=now,
        native=True,
        retry=existing is not None,
    )
    if not budget["ok"]:
        return RepairDecision(budget["reason"], cycle_id)
    if cycle is None:
        cycle_id = f"repair-{uuid4().hex}"
        write.insert_native_cycle(
            conn,
            cycle_id=cycle_id,
            source_id=claim.source_id,
            pr_source_id=observation.pr_source_id,
            base_ref=scope.base_branch,
            policy_json=json.dumps(effective, sort_keys=True, allow_nan=False),
            now=now,
        )
        write.observe_native_cycle(
            conn,
            cycle_id=cycle_id,
            status="active",
            reason=None,
            observed_at=observation.observed_at,
            observation_digest=digest(asdict(observation)),
            now=now,
        )
    if existing:
        return RepairDecision("already_claimed", cycle_id)
    if not write.record_native_claim(
        conn,
        cycle_id=cycle_id,
        request_id=request_id,
        episode_id=episode_id,
        task_id=task_id,
        round_id=round_id,
        now=now,
    ):
        raise RuntimeError("Native repair claim changed under the source writer")
    return RepairDecision("claimed", cycle_id)


def record_usage(conn, claim, *, cycle_id, attempt_id, tokens, cost_usd, now):
    """Save controller-measured attempt usage without replacing known values."""
    if (
        not conn.in_transaction
        or utc_timestamp(now) is None
        or (tokens is not None and (type(tokens) is not int or tokens < 0))
        or (cost_usd is not None and (type(cost_usd) not in (int, float) or cost_usd < 0 or not isfinite(cost_usd)))
        or not source_sessions.lock_claim(conn, claim, now=now)
    ):
        return False
    cycle = read.load_cycle(conn, cycle_id)
    if cycle is None or cycle["source_id"] != claim.source_id:
        return False
    return write.record_native_usage(
        conn, cycle_id=cycle_id, attempt_id=attempt_id, tokens=tokens, cost_usd=cost_usd, observed_at=now
    )
