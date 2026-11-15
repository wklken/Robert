"""Current authority reads for one exact taskless mixed-source model check."""

from contextlib import closing
from dataclasses import dataclass
import json
import subprocess
import time
from typing import Callable
from uuid import uuid4

from robert_agent.foundation.contracts.execution import ModelRunStatus
from robert_agent.foundation.contracts.work import SourceCapability
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.foundation.values import utc_now
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import source_results, source_sessions as rounds
from robert_agent.persistence.rounds.source_evidence import context_digest, json_object, model_identity_facts
from robert_agent.persistence.stores import source_sessions
from robert_agent.work.application import (
    admission,
    source_github_observation as github,
    source_observation,
    tapd_intake,
)
from robert_agent.work.tapd_authority import PublicationFacts, TicketIdentity

# Shorter than the model lease. Every read in the check shares this bound.
_OBSERVATION_SECONDS = 25


@dataclass(frozen=True, slots=True)
class _ObservationStop:
    external_set: Callable[[], bool]
    deadline: float

    def is_set(self):
        return self.external_set() or time.monotonic() >= self.deadline


@dataclass(frozen=True, slots=True)
class ModelAuthorityObservation:
    proof: github.MixedModelProof
    publication: PublicationFacts
    deadline: float


def check_target(round_, binding, run, *, request_id, stage, launch_id, check):
    """Bind the planned input or the complete durable launch to this invocation."""
    return github.ModelObservationTarget(
        round_["round_id"],
        binding["run_id"],
        request_id,
        stage,
        binding["input_version"],
        round_["input_sha256"],
        binding["prompt_sha256"],
        launch_id,
        uuid4().hex,
        check,
        context_digest(dict(run) if run else None),
    )


def _run_matches(conn, claim, current, target):
    if (
        current["source_id"] != claim.source_id
        or current["capability"] != SourceCapability.READONLY
        or current["round_id"] != target.round_id
        or current["input_version"] != target.input_version
        or current["input_sha256"] != target.input_sha256
        or not source_sessions.pending_member(conn, target.round_id, target.request_id)
    ):
        return False
    run = rounds.model_run(conn, target.run_id)
    if context_digest(dict(run) if run else None) != target.run_sha256:
        return False
    if run is None:
        return target.check == "initial"
    if any(
        run[key] != getattr(target, key)
        for key in ("round_id", "request_id", "stage", "input_version", "prompt_sha256")
    ):
        return False
    if target.check == "initial":
        return run["launch_id"] is None and run["status"] in (ModelRunStatus.WAITING, ModelRunStatus.STARTING)
    if run["slot_reserved"] != 1 or run["launch_id"] != target.launch_id or run["launch_token"] != claim.token:
        return False
    if target.check == "pre_identity":
        return run["status"] == ModelRunStatus.STARTING and run["dispatch_json"] is None
    return (
        target.check in {"started", "periodic", "final"}
        and run["status"] == ModelRunStatus.RUNNING
        and model_identity_facts(json_object(run["dispatch_json"])) is not None
    )


@dataclass(frozen=True, slots=True)
class MixedModelObserver:
    db_path: object
    config: dict
    ticket: TicketIdentity
    claim: source_sessions.SourceClaim
    read_ticket: Callable
    github_runner: Callable | None = None

    def observe(self, target, *, external, deadline=None):
        """Read TAPD and each GitHub member outside writers before this check."""
        expires = time.monotonic() + _OBSERVATION_SECONDS
        stop = _ObservationStop(external.is_set, min(expires, deadline) if deadline is not None else expires)
        if stop.is_set():
            return None
        begin = utc_timestamp(utc_now())
        try:
            refreshed = self.read_ticket(self.ticket, read_stop=stop)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        if not isinstance(refreshed, admission.SourceDispatchObservation) or refreshed.claim != self.claim:
            return None
        seen = utc_timestamp(refreshed.observation.observed_at)
        if (
            refreshed.observation.facts.identity != self.ticket
            or refreshed.publication.ticket != self.ticket
            or seen is None
            or not begin <= seen <= utc_timestamp(utc_now())
        ):
            return None
        with closing(database.connect(self.db_path, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            now = utc_now()
            current = source_sessions.lock_round(conn, self.claim, target.round_id, now=now)
            publication = source_observation.publication_facts(conn, self.ticket)
            if (
                current is None
                or not _run_matches(conn, self.claim, current, target)
                or publication != refreshed.publication
            ):
                return None
            active = github.active_scope(conn, current)
            if active is None:
                return None
            observed = tapd_intake.observe_ticket(
                conn, self.config["tapd"], refreshed.observation, publication, now=now
            )
            if not observed.revision:
                return None

        operations, _, failure = github.read_operations(
            self.db_path,
            self.config,
            self.claim,
            self.ticket,
            revision=observed.revision,
            request_ids=active.request_ids,
            runner=self.github_runner,
            stop=stop,
            active=active,
            target=target,
        )
        if not operations:
            return None

        with closing(database.connect(self.db_path, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            now = utc_now()
            current = source_sessions.lock_round(conn, self.claim, target.round_id, now=now)
            if (
                current is None
                or not _run_matches(conn, self.claim, current, target)
                or source_observation.publication_facts(conn, self.ticket) != publication
            ):
                return None
            checked = tapd_intake.refresh_mixed_round(
                conn,
                self.config,
                self.ticket,
                self.claim,
                active=active,
                revision=observed.revision,
                operations=operations,
                replacement_id=None,
                now=now,
                target=target,
            )
        if failure or stop.is_set() or checked.model_proof is None:
            return None
        return ModelAuthorityObservation(checked.model_proof, publication, stop.deadline)

    def authorized(self, conn, claim, current, target, observation, *, now):
        """Use only the just-read dedicated model proof and exact current model row."""
        if (
            claim != self.claim
            or not isinstance(observation, ModelAuthorityObservation)
            or time.monotonic() >= observation.deadline
            or not _run_matches(conn, claim, current, target)
            or observation.publication.unknown_intents
            or source_observation.publication_facts(conn, self.ticket) != observation.publication
            or source_results.unknown_github_effect(conn, self.ticket.source_id)
        ):
            return False
        revision = observation.proof.revision
        decisions = tapd_intake.current_mixed_model_grants(
            conn,
            self.config,
            self.ticket,
            claim,
            current,
            revision=revision,
            proof=observation.proof,
            now=now,
            target=target,
        )
        if decisions is None:
            return False
        repo_ids = json.loads(current["repo_ids_json"])
        repos = [github.configured_repository(conn, self.config, self.ticket, repo_id) for repo_id in repo_ids]
        if not repos or any(repo is None for repo in repos):
            return False
        names = {repo["full_name"] for repo in repos}
        for member in source_sessions.pending_round_members(conn, current["round_id"]):
            request_id = member["request_id"]
            decision = decisions.get(request_id) or tapd_intake.revalidate_request(
                conn, self.config["tapd"], self.ticket, request_id, revision=revision
            )
            if (
                decision.reason != "allowed"
                or decision.capability != SourceCapability.READONLY
                or not names <= set(decision.repos)
            ):
                return False
        return time.monotonic() < observation.deadline
