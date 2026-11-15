"""Observe exact mixed accepted-root action authority without starting an effect."""

import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass

from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.foundation.values import utc_now
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import source_results
from robert_agent.persistence.stores import source_sessions
from robert_agent.work.application import source_github_observation as github
from robert_agent.work.application import source_observation, tapd_intake
from robert_agent.work.tapd_authority import PublicationFacts

_GITHUB_OBSERVATION_SECONDS = 60


@dataclass(frozen=True, slots=True)
class _ReadStop:
    external_set: Callable[[], bool] | None
    deadline: float

    def is_set(self):
        return time.monotonic() >= self.deadline or (self.external_set is not None and self.external_set())


@dataclass(frozen=True, slots=True)
class AcceptedDeliveryObservation:
    github: github.RootDeliveryProof
    publication: PublicationFacts
    tapd_observed_at: str
    deadline: float


@dataclass(frozen=True, slots=True)
class AcceptedCompletionObservation:
    github: github.RootCompletionProof
    publication: PublicationFacts
    tapd_observed_at: str
    deadline: float


def observe(
    db_path,
    config,
    ticket,
    claim,
    *,
    root_id,
    action_id,
    snapshot,
    runner=None,
    stop=None,
    code_effect=None,
    feedback_effect=None,
):
    """Persist the live TAPD observation then read the target's original GitHub items without a writer."""

    def scope_of(conn):
        return github.accepted_root_scope(
            conn, root_id, action_id, code_effect=code_effect, feedback_effect=feedback_effect
        )

    read = _observe_root(db_path, config, ticket, claim, root_id, snapshot, scope_of, runner, stop)
    if read is None:
        return None
    scope, revision, operations, publication, deadline = read
    return AcceptedDeliveryObservation(
        github.RootDeliveryProof(scope, claim, revision, operations), publication, snapshot.observed_at, deadline
    )


def observe_completion(db_path, config, ticket, claim, *, scope, snapshot, runner=None, stop=None):
    """Read all original discussion members for the exact completed receipt vector."""

    if not isinstance(scope, github.AcceptedCompletionScope):
        return None

    def scope_of(conn):
        return scope if github.accepted_completion_scope(conn, scope.root_id) == scope else None

    read = _observe_root(db_path, config, ticket, claim, scope.root_id, snapshot, scope_of, runner, stop)
    if read is None:
        return None
    scope, revision, operations, publication, deadline = read
    return AcceptedCompletionObservation(
        github.RootCompletionProof(scope, claim, revision, operations), publication, snapshot.observed_at, deadline
    )


def _observe_root(db_path, config, ticket, claim, root_id, snapshot, scope_of, runner, stop):
    if snapshot.facts.identity != ticket:
        return None
    read_stop = _ReadStop(stop.is_set if stop is not None else None, time.monotonic() + _GITHUB_OBSERVATION_SECONDS)
    if read_stop.is_set():
        return None
    with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
        transactions.begin_immediate(conn)
        now = utc_now()
        scope = scope_of(conn)
        root = source_results.by_id(conn, root_id)
        observed = utc_timestamp(snapshot.observed_at)
        accepted = utc_timestamp(root["accepted_at"]) if root else None
        if (
            scope is None
            or scope.source_id != ticket.source_id
            or claim.source_id != ticket.source_id
            or not source_sessions.lock_claim(conn, claim, now=now)
            or observed is None
            or accepted is None
            or not accepted < observed <= utc_timestamp(now)
        ):
            return None
        publication = source_observation.publication_facts(conn, ticket)
        current = tapd_intake.observe_ticket(conn, config["tapd"], snapshot, publication, now=now)
        scope = scope_of(conn)
        if scope is None or not current.revision:
            return None
    operations, _, _failure = github.read_operations(
        db_path,
        config,
        claim,
        ticket,
        revision=current.revision,
        request_ids=scope.request_ids,
        runner=runner,
        stop=read_stop,
        delivery=scope,
    )
    if not operations:
        return None
    return scope, current.revision, operations, publication, read_stop.deadline


def matches(
    conn,
    observation,
    ticket,
    claim,
    root_id,
    action_id,
    *,
    revision,
    snapshot=None,
    publication=None,
    code_effect=None,
    feedback_effect=None,
):
    """Match one current action's exact revision, source fence, and observation packet."""
    if not isinstance(observation, AcceptedDeliveryObservation):
        return False
    proof = observation.github
    return (
        isinstance(proof, github.RootDeliveryProof)
        and isinstance(proof.scope, github.AcceptedRootScope)
        and proof.claim == claim
        and proof.scope.source_id == ticket.source_id
        and proof.scope.root_id == root_id
        and proof.scope.action_id == action_id
        and proof.revision == revision
        and proof.scope.code_effect == code_effect
        and proof.scope.feedback_effect == feedback_effect
        and (snapshot is None or observation.tapd_observed_at == snapshot.observed_at)
        and observation.publication
        == (source_observation.publication_facts(conn, ticket) if publication is None else publication)
        and github.accepted_root_scope(
            conn, root_id, action_id, code_effect=code_effect, feedback_effect=feedback_effect
        )
        == proof.scope
    )


def matches_completion(conn, observation, ticket, claim, root_id, *, revision):
    """Verify the current completion packet without converting it into delivery authority."""
    if not isinstance(observation, AcceptedCompletionObservation):
        return False
    proof = observation.github
    return (
        isinstance(proof, github.RootCompletionProof)
        and isinstance(proof.scope, github.AcceptedCompletionScope)
        and proof.claim == claim
        and proof.scope.root_id == root_id
        and proof.scope.source_id == ticket.source_id
        and proof.revision == revision
        and is_current(observation)
        and github.revision_current(conn, ticket, revision)
        and observation.publication == source_observation.publication_facts(conn, ticket)
        and github.accepted_completion_scope(conn, root_id) == proof.scope
    )


def is_current(observation):
    return time.monotonic() < observation.deadline
