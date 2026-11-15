"""Current, operation-bound GitHub evidence for canonical source requests."""

import json
import time
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, replace
from hashlib import sha256

from robert_agent.adapters.github import discovery
from robert_agent.adapters.github.discussion import DiscussionReader, ItemRead
from robert_agent.foundation import values
from robert_agent.foundation.contracts.delivery import PublicationStatus, PublishStatus, SourceResultKind
from robert_agent.foundation.contracts.work import RequestEventState, SourceIngressPhase
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import (
    source_confirmation,
    source_delivery_pauses,
    source_delivery_recovery,
    source_feedback,
    source_github_publication,
    source_members,
    source_results,
    source_retirement,
    source_tasks,
)
from robert_agent.persistence.queries import (
    source_github_ingress as read,
)
from robert_agent.persistence.queries import (
    source_sessions as round_queries,
)
from robert_agent.persistence.queries import (
    tapd_intake as intake_queries,
)
from robert_agent.persistence.rounds.source_evidence import context_digest, json_object
from robert_agent.persistence.stores import source_github_ingress as write
from robert_agent.persistence.stores import source_sessions
from robert_agent.work import source_pr_selection

BATCH_LIMIT = 4


@dataclass(frozen=True, slots=True)
class CurrentEvidence:
    request_id: str
    status: str
    reason: str
    observed_at: str
    item: ItemRead
    semantic_version: str | None = None
    parent: dict | None = None
    disposition: str | None = None


@dataclass(frozen=True, slots=True)
class ObservationBatch:
    operation_id: str | None
    reason: str
    evidence: tuple[CurrentEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class ActiveRoundScope:
    source_id: str
    round_id: str
    input_version: str
    input_sha256: str
    member_digest: str
    request_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkerObservationTarget:
    task_id: str
    attempt_id: str
    prepared_sha256: str
    tapd_observed_at: str


@dataclass(frozen=True, slots=True)
class MixedWorkerProof:
    active: ActiveRoundScope
    claim: source_sessions.SourceClaim
    revision: str
    operations: tuple[tuple[str, tuple[str, ...]], ...]
    target: WorkerObservationTarget | None = None


@dataclass(frozen=True, slots=True)
class ModelObservationTarget:
    round_id: str
    run_id: str
    request_id: str
    stage: str
    input_version: str
    input_sha256: str
    prompt_sha256: str
    launch_id: str
    check_id: str
    check: str
    run_sha256: str


@dataclass(frozen=True, slots=True)
class MixedModelProof:
    active: ActiveRoundScope
    claim: source_sessions.SourceClaim
    revision: str
    operations: tuple[tuple[str, tuple[str, ...]], ...]
    target: ModelObservationTarget


@dataclass(frozen=True, slots=True)
class CodeTarget:
    repository: str
    base_branch: str
    base_sha: str
    pr_number: int | None
    head_repository: str
    head_branch: str
    initial_head: str | None


@dataclass(frozen=True, slots=True)
class CodePrestart:
    request_fingerprint: str
    target: CodeTarget


@dataclass(frozen=True, slots=True)
class CodeStarted:
    publication_id: str
    request_fingerprint: str
    target: CodeTarget


def _code_target(packet):
    effect = packet["effect"]
    return CodeTarget(
        effect["repo"].casefold(),
        effect["base"],
        effect["base_sha"],
        effect.get("pr_number"),
        effect["head_repository"].casefold(),
        effect["branch"],
        effect.get("expected_remote_sha"),
    )


def code_prestart(packet):
    """Bind the original audited packet before the intent starts."""
    return CodePrestart(context_digest(packet), _code_target(packet))


def code_started(publication_id, packet):
    """Bind the exact started intent and its independently persisted packet."""
    return CodeStarted(publication_id, context_digest(packet), _code_target(packet))


def code_parent_current(boundary, parent):
    """Honor newer discussion parent facts about the code target."""
    if boundary is None:
        return True
    if not isinstance(parent, dict) or not isinstance(parent.get("base_repository"), str):
        return False
    target = boundary.target
    if parent.get("base_repository") != target.repository:
        return True
    if target.pr_number is None:
        return parent.get("base_branch") != target.base_branch or parent.get("base_sha") == target.base_sha
    if parent.get("number") != target.pr_number:
        return True
    return (
        parent.get("state") == "open"
        and parent.get("base_branch") == target.base_branch
        and parent.get("base_sha") == target.base_sha
        and parent.get("head_repository") == target.head_repository
        and parent.get("head_branch") == target.head_branch
        and parent.get("head_sha") == target.initial_head
    )


@dataclass(frozen=True, slots=True)
class FeedbackPrestart:
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class AcceptedRootScope:
    source_id: str
    round_id: str
    root_id: str
    action_id: str
    position: int
    commitment: str
    request_ids: tuple[str, ...]
    code_effect: CodePrestart | CodeStarted | None = None
    feedback_effect: FeedbackPrestart | None = None


@dataclass(frozen=True, slots=True)
class RootDeliveryProof:
    scope: AcceptedRootScope
    claim: source_sessions.SourceClaim
    revision: str
    operations: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True, slots=True)
class AcceptedCompletionScope:
    source_id: str
    round_id: str
    root_id: str
    commitment: str
    request_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RootCompletionProof:
    scope: AcceptedCompletionScope
    claim: source_sessions.SourceClaim
    revision: str
    operations: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True, slots=True)
class AcceptedDispositionScope:
    source_id: str
    round_id: str
    root_id: str
    commitment: str
    request_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RootDispositionProof:
    scope: AcceptedDispositionScope
    claim: source_sessions.SourceClaim
    revision: str
    operations: tuple[tuple[str, tuple[str, ...]], ...]
    deadline: float


def accepted_disposition_scope(conn, root_id):
    """Bind the original accepted root even after cancellation, without granting delivery."""
    root = source_results.by_id(conn, root_id)
    if root is None or root["kind"] not in (SourceResultKind.ANSWER, SourceResultKind.QUESTION):
        return None
    source_id, round_id = root["source_id"], root["round_id"]
    round_ = source_results.delivery_round(conn, source_id, round_id)
    if (
        round_ is None
        or round_["status"] not in ("active", "canceled")
        or source_retirement.for_root(conn, root_id) is not None
        or round_["input_sha256"] != (json_object(root["evidence_json"]) or {}).get("round_input_sha256")
        or sha256(round_["input_snapshot_json"].encode()).hexdigest() != round_["input_sha256"]
    ):
        return None
    cancellation = intake_queries.round_cancellation(conn, round_id)
    if round_["status"] == "canceled" and cancellation is None:
        return None
    github = source_members.round_github_request_ids(conn, round_id)
    members = source_members.disposition_members(conn, root, round_) if github else ()
    actions = source_results.required_actions(conn, root_id)
    if (
        not members
        or tuple(item.member["request_id"] for item in members if item.ownership.return_target) != github
        or source_results.required_action_sets(conn, root, actions) is None
    ):
        return None
    pause = source_delivery_pauses.latest(conn, root_id)
    recovery = source_delivery_recovery.for_generation(conn, root_id, pause["generation"]) if pause else None
    commitment = context_digest(
        [
            dict(root),
            dict(round_),
            dict(cancellation) if cancellation else None,
            dict(pause) if pause else None,
            dict(recovery) if recovery else None,
            [dict(row) for row in actions],
            [
                [dict(item.member), dict(item.ownership.request), [dict(event) for event in item.ownership.events]]
                for item in members
            ],
        ]
    )
    return AcceptedDispositionScope(source_id, round_id, root_id, commitment, github)


@dataclass(frozen=True, slots=True)
class AcceptedRecoveryScope:
    source_id: str
    round_id: str
    root_id: str
    generation: int
    question_id: str
    transition_id: str
    action_id: str
    purpose: str
    commitment: str
    request_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RootRecoveryProof:
    scope: AcceptedRecoveryScope
    claim: source_sessions.SourceClaim
    revision: str
    operations: tuple[tuple[str, tuple[str, ...]], ...]
    deadline: float


@dataclass(frozen=True, slots=True)
class RecoveredTapdScope(AcceptedRecoveryScope):
    snapshot_sha256: str


@dataclass(frozen=True, slots=True)
class RootRecoveredTapdProof(RootRecoveryProof):
    scope: RecoveredTapdScope


def accepted_recovery_scope(conn, root_id, *, generation, question_id, transition_id, action_id):
    """Bind positive recovery reads to the exact before-choice or applied owner."""
    original = accepted_disposition_scope(conn, root_id)
    pause = source_delivery_pauses.latest(conn, root_id)
    if original is None or pause is None or pause["generation"] != generation:
        return None
    if (
        round_queries.active_round(conn, original.source_id) is not None
        or round_queries.blockers_for_publication(conn, original.source_id, root_id)
        or source_results.unknown_github_effect(conn, original.source_id, require_positive_receipt=True)
        or source_results.tapd_publication_evidence(conn, original.source_id).unknown_intents
    ):
        return None
    owner = source_delivery_recovery.for_generation(conn, root_id, generation)
    if owner is None:
        question = source_confirmation.current(conn, original.source_id)
        durable = source_delivery_recovery.bound_delivery(conn, pause)
        purpose = "claim"
    else:
        if (
            source_delivery_recovery.active(conn, original.source_id) != owner
            or owner["transition_id"] != transition_id
            or owner["question_id"] != question_id
        ):
            return None
        question = source_confirmation.question(conn, original.source_id, question_id)
        durable = source_delivery_recovery.applied_bound(conn, owner)
        purpose = "owned"
    answer = source_confirmation.answer(conn, question_id)
    pending = tuple(
        action["action_id"]
        for action in source_results.required_actions(conn, root_id)
        if action["publish_status"] != PublishStatus.PUBLISHED
    )
    if (
        question is None
        or question["question_id"] != question_id
        or answer is None
        or answer["consumer_id"] != (None if owner is None else transition_id)
        or durable is None
        or not pending
        or pending[0] != action_id
        or not set(pending) <= set(durable.missing)
        or (owner is None and source_delivery_pauses.dormant_actions(conn, pause) != pending)
    ):
        return None
    commitment = context_digest(
        [
            purpose,
            original.commitment,
            durable.digest,
            dict(question),
            dict(answer),
            dict(owner) if owner else None,
            generation,
            action_id,
            transition_id,
        ]
    )
    return AcceptedRecoveryScope(
        original.source_id,
        original.round_id,
        root_id,
        generation,
        question_id,
        transition_id,
        action_id,
        purpose,
        commitment,
        original.request_ids,
    )


def _current_recovery_scope(conn, scope):
    return accepted_recovery_scope(
        conn,
        scope.root_id,
        generation=scope.generation,
        question_id=scope.question_id,
        transition_id=scope.transition_id,
        action_id=scope.action_id,
    )


def recovered_tapd_scope(conn, owner, snapshot_sha256):
    """Authorize a separate operation over the next missing TAPD comment only."""
    if (
        type(owner) is not AcceptedRecoveryScope
        or owner.purpose != "owned"
        or _current_recovery_scope(conn, owner) != owner
    ):
        return None
    root = source_results.for_source(conn, owner.source_id, owner.root_id)
    source = source_results.tapd_source(conn, owner.source_id)
    if root is None or source is None:
        return None
    target = source_results.source_target(source)
    action = next(
        (row for row in source_results.required_actions(conn, owner.root_id) if row["action_id"] == owner.action_id),
        None,
    )
    if (
        action is None
        or action["provider"] != "tapd"
        or action["action_type"] != "comment"
        or json_object(action["target_json"]) != target
        or not source_delivery_pauses.locally_never_started(conn, root, action, target)
    ):
        return None
    return RecoveredTapdScope(
        **{
            **asdict(owner),
            "purpose": "tapd_effect",
            "commitment": context_digest([owner.commitment, snapshot_sha256]),
            "snapshot_sha256": snapshot_sha256,
        }
    )


def _current_recovered_tapd_scope(conn, scope):
    return recovered_tapd_scope(conn, _current_recovery_scope(conn, scope), scope.snapshot_sha256)


def observe_recovery(db_path, config, claim, ticket, scope, *, revision, runner=None, stop=None):
    """Read all original discussions for initial or already-owned recovery."""
    if type(scope) is not AcceptedRecoveryScope:
        return None
    return _observe_recovery(
        db_path,
        config,
        claim,
        ticket,
        scope,
        RootRecoveryProof,
        revision=revision,
        runner=runner,
        stop=stop,
    )


def observe_recovered_tapd(db_path, config, claim, ticket, scope, *, revision, runner=None, stop=None):
    """Read all original discussions for the exact unstarted recovered TAPD effect."""
    if type(scope) is not RecoveredTapdScope:
        return None
    return _observe_recovery(
        db_path,
        config,
        claim,
        ticket,
        scope,
        RootRecoveredTapdProof,
        revision=revision,
        runner=runner,
        stop=stop,
    )


def _observe_recovery(db_path, config, claim, ticket, scope, proof_type, *, revision, runner, stop):
    if scope.source_id != ticket.source_id:
        return None
    deadline = min(time.monotonic() + 60, getattr(stop, "deadline", float("inf")))
    bounded = _ObservationStop(stop, deadline)
    operations, _, _ = read_operations(
        db_path,
        config,
        claim,
        ticket,
        revision=revision,
        request_ids=scope.request_ids,
        runner=runner,
        stop=bounded,
        delivery=scope,
    )
    if not operations or bounded.is_set():
        return None
    return proof_type(scope, claim, revision, operations, deadline)


@dataclass(frozen=True, slots=True)
class _ObservationStop:
    upstream: object
    deadline: float

    def is_set(self):
        return time.monotonic() >= self.deadline or (self.upstream is not None and self.upstream.is_set())


def observe_disposition(db_path, config, claim, ticket, scope, *, revision, runner=None, stop=None):
    """Read exact original GitHub discussions for one current or historical result."""
    if not isinstance(scope, AcceptedDispositionScope) or scope.source_id != ticket.source_id:
        return None
    deadline = min(time.monotonic() + 60, getattr(stop, "deadline", float("inf")))
    bounded = _ObservationStop(stop, deadline)
    operations, _, _ = read_operations(
        db_path,
        config,
        claim,
        ticket,
        revision=revision,
        request_ids=scope.request_ids,
        runner=runner,
        stop=bounded,
        delivery=scope,
    )
    if not operations or bounded.is_set():
        return None
    return RootDispositionProof(scope, claim, revision, operations, deadline)


def _accepted_subject(conn, root_id):
    root = source_results.by_id(conn, root_id)
    if root is None or root["kind"] not in (SourceResultKind.ANSWER, SourceResultKind.QUESTION):
        return None
    active = round_queries.active_round(conn, root["source_id"])
    evidence = json_object(root["evidence_json"]) or {}
    if (
        active is None
        or active["round_id"] != root["round_id"]
        or active["input_version"] != root["input_version"]
        or active["input_sha256"] != evidence.get("round_input_sha256")
        or sha256(active["input_snapshot_json"].encode()).hexdigest() != active["input_sha256"]
        or intake_queries.round_cancellation(conn, root["round_id"]) is not None
    ):
        return None
    github = source_members.round_github_request_ids(conn, root["round_id"])
    members = source_members.accepted_round_members(conn, root, active) if github else ()
    if (
        not members
        or tuple(member.member["request_id"] for member in members if member.ownership.return_target) != github
    ):
        return None
    candidate = (
        source_results.worker_candidate(conn, root["worker_result_id"])
        if root["worker_result_id"] is not None
        else round_queries.model_run(conn, root["model_run_id"])
    )
    if candidate is None:
        return None
    ownership = [
        [dict(member.member), dict(member.ownership.request), [dict(event) for event in member.ownership.events]]
        for member in members
    ]
    return root, github, (dict(root), dict(active), dict(candidate)), ownership


def accepted_completion_scope(conn, root_id):
    """Bind a locally completable root to the vector of all canonical receipts."""
    subject = _accepted_subject(conn, root_id)
    if subject is None:
        return None
    root, github, commitment, ownership = subject
    source = source_results.tapd_source(conn, root["source_id"])
    if source is None:
        return None
    receipts = source_results.confirmed_required_effects(conn, root_id, source_results.source_target(source))
    if not receipts:
        return None
    return AcceptedCompletionScope(
        root["source_id"],
        root["round_id"],
        root_id,
        context_digest([*commitment, source_results.receipt_vector(receipts), ownership]),
        github,
    )


def accepted_root_scope(conn, root_id, action_id, *, code_effect=None, feedback_effect=None):
    """Bind an exact accepted effect to its provider, owner and transition."""
    if feedback_effect is not None and (not isinstance(feedback_effect, FeedbackPrestart) or code_effect is not None):
        return None

    subject = _accepted_subject(conn, root_id)
    if subject is None:
        return None
    root, github, commitment, ownership = subject
    actions = source_results.required_actions(conn, root_id)
    sets = source_results.required_action_sets(conn, root, actions)
    if sets is None:
        return None
    base, feedback = sets
    candidates = base if feedback_effect is None else feedback
    action = next((row for row in candidates if row["action_id"] == action_id), None)
    if (
        action is None
        or action["publish_status"] != PublishStatus.NOT_PUBLISHED
        or action["external_id"] is not None
        or action["target_url"] is not None
        or action["intent_finished_at"] is not None
        or not _accepts_effect(conn, root_id, action, code_effect, feedback_effect)
    ):
        return None
    return AcceptedRootScope(
        root["source_id"],
        root["round_id"],
        root_id,
        action_id,
        action["position"],
        context_digest([*commitment, [dict(row) for row in actions], ownership]),
        github,
        code_effect,
        feedback_effect,
    )


def _accepts_effect(conn, root_id, action, code_effect, feedback_effect):
    if feedback_effect is not None:
        if action["intent_started_at"] is not None or action["intent_status"] not in (None, PublicationStatus.PREPARED):
            return False
        if action["publication_id"] is None:
            return action["intent_status"] is None
        row = source_feedback.required_action(conn, root_id, action["action_id"])
        return (
            row is not None
            and action["intent_status"] == PublicationStatus.PREPARED
            and row["request_fingerprint"] == feedback_effect.request_fingerprint
            and source_feedback.prepared_request(conn, row) is not None
        )
    if code_effect is None:
        return (
            action["provider"] == "tapd"
            and action["action_type"] == "comment"
            and action["intent_started_at"] is None
            and action["intent_status"] in (None, PublicationStatus.PREPARED)
        )
    if not isinstance(code_effect, (CodePrestart, CodeStarted)):
        return False
    native = source_github_publication.required_action(conn, root_id, action["action_id"])
    if native is None:
        return False
    if isinstance(code_effect, CodePrestart) and action["publication_id"] is None:
        return action["intent_status"] is None and action["intent_started_at"] is None
    saved = source_github_publication.prepared_request(native)
    if (
        saved is None
        or native["request_fingerprint"] != code_effect.request_fingerprint
        or context_digest(saved) != code_effect.request_fingerprint
        or _code_target(saved) != code_effect.target
    ):
        return False
    if isinstance(code_effect, CodePrestart):
        return action["intent_status"] == PublicationStatus.PREPARED and action["intent_started_at"] is None
    return (
        action["publication_id"] == code_effect.publication_id
        and action["intent_status"] == PublicationStatus.EXECUTING
        and utc_timestamp(action["intent_started_at"]) is not None
    )


def active_scope(conn, round_):
    """Bind all canonical GitHub members to this exact unaccepted execution round."""
    if round_ is None:
        return None
    source_id, round_id = round_["source_id"], round_["round_id"]
    if (
        round_queries.active_round(conn, source_id) != round_
        or source_results.for_round(conn, round_id) is not None
        or intake_queries.round_cancellation(conn, round_id) is not None
        or sha256(round_["input_snapshot_json"].encode()).hexdigest() != round_["input_sha256"]
    ):
        return None
    github = source_members.round_github_request_ids(conn, round_id)
    if not github:
        return None
    members = round_queries.round_requests(conn, round_id)
    return ActiveRoundScope(
        source_id,
        round_id,
        round_["input_version"],
        round_["input_sha256"],
        context_digest([dict(round_), [dict(member) for member in members]]),
        github,
    )


@dataclass(frozen=True, slots=True)
class _Binding:
    target: dict
    lineage: object
    repo: dict
    creator: str
    created_at: str
    updated_at: str
    scope_digest: str


@contextmanager
def _writer(db_path):
    with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
        transactions.begin_immediate(conn)
        yield conn


def revision_current(conn, ticket, revision):
    row = read.source(conn, ticket.source_id)
    metadata = json_object(row["metadata_json"]) if row else None
    return bool(
        revision
        and metadata
        and metadata.get("revision") == revision
        and row["provider"] == "tapd"
        and (row["host"], row["project_key"], row["source_type"], row["external_id"]) == ticket.key
    )


def configured_repository(conn, config, ticket, repo_id):
    """Resolve the exact current scoped GitHub repository and its trust configuration."""
    tapd = config.get("tapd") or {}
    if tapd.get("enabled") is not True or tapd.get("host") != ticket.host:
        return None
    stored = source_tasks.repository(conn, repo_id)
    if stored is None or not stored["enabled"]:
        return None
    name = stored["full_name"]
    projects = [project for project in tapd.get("projects", ()) if project["workspace_id"] == ticket.workspace_id]
    if len(projects) != 1 or name not in projects[0]["repositories"]:
        return None
    fields = ("full_name", "github_account", "default_base_branch", "repo_root", "worktree_root")
    repos = [repo for repo in config.get("repos", ()) if repo["full_name"] == name]
    if (
        len(repos) != 1
        or repos[0].get("enabled", True) is not True
        or any(repos[0].get(field) != stored[field] for field in fields)
    ):
        return None
    return {
        **{field: stored[field] for field in fields},
        "trusted_actors": sorted(repos[0].get("trusted_actors", ())),
        "project": projects[0],
        "tapd_identity": {key: tapd.get(key) for key in ("host", "bot_name", "reply_name", "api_timezone")},
    }


def _binding(conn, config, claim, ticket, revision, request_id):
    ownership = source_members.owned_request(conn, ticket.source_id, request_id)
    if (
        ownership is None
        or ownership.return_target is None
        or ownership.request["phase"]
        not in (SourceIngressPhase.HELD.value, SourceIngressPhase.PENDING.value, SourceIngressPhase.PAUSED.value)
        or ownership.events[0]["state"] != RequestEventState.PENDING.value
    ):
        return None
    return _bound_owner(conn, config, claim, ticket, revision, ownership)


def _bound_owner(conn, config, claim, ticket, revision, ownership):
    bridge, request, lineage, target, item = (
        ownership.bridge,
        ownership.request,
        ownership.creation,
        ownership.return_target,
        ownership.original_item,
    )
    repo = configured_repository(conn, config, ticket, request["repo_id"])
    saved = json_object(request["authorization_json"]) or {}
    if (
        repo is None
        or repo["full_name"] != target["repository"]
        or not isinstance(repo["github_account"], str)
        or not repo["github_account"]
        or saved.get("bot_login") != repo["github_account"]
    ):
        return None
    binding = {
        "claim": asdict(claim),
        "revision": revision,
        "bridge": {
            field: bridge[field]
            for field in ("event_id", "target_json", "first_observation_json", "latest_observation_json", "observed_at")
        },
        "request": dict(request),
        "repo": repo,
        "lineage": asdict(lineage),
    }
    return _Binding(
        target, lineage, repo, item["author"], item["created_at"], item["updated_at"], context_digest(binding)
    )


def _bindings(conn, config, claim, ticket, revision, request_ids, active, target, delivery):
    if claim.source_id != ticket.source_id or not revision_current(conn, ticket, revision):
        return None
    if delivery is not None:
        if isinstance(delivery, RecoveredTapdScope):
            current_scope = _current_recovered_tapd_scope(conn, delivery)
        elif isinstance(delivery, AcceptedRecoveryScope):
            current_scope = _current_recovery_scope(conn, delivery)
        elif isinstance(delivery, AcceptedDispositionScope):
            current_scope = accepted_disposition_scope(conn, delivery.root_id)
        elif isinstance(delivery, AcceptedCompletionScope):
            current_scope = accepted_completion_scope(conn, delivery.root_id)
        elif isinstance(delivery, AcceptedRootScope):
            current_scope = accepted_root_scope(
                conn,
                delivery.root_id,
                delivery.action_id,
                code_effect=delivery.code_effect,
                feedback_effect=delivery.feedback_effect,
            )
        else:
            return None
        if (
            active is not None
            or target is not None
            or delivery.source_id != ticket.source_id
            or delivery != current_scope
            or not set(request_ids) <= set(delivery.request_ids)
        ):
            return None
        root = source_results.by_id(conn, delivery.root_id)
        current = source_results.delivery_round(conn, delivery.source_id, delivery.round_id)
        members = (
            source_members.disposition_members(conn, root, current)
            if isinstance(delivery, (AcceptedDispositionScope, AcceptedRecoveryScope))
            else source_members.accepted_round_members(conn, root, current)
        )
        owned = {member.member["request_id"]: member.ownership for member in members if member.ownership.return_target}
        return tuple(
            _bound_owner(conn, config, claim, ticket, revision, owned[request_id]) for request_id in request_ids
        )
    if target is not None and (
        active is None or not isinstance(target, (WorkerObservationTarget, ModelObservationTarget))
    ):
        return None
    if active is not None and (
        active.source_id != ticket.source_id
        or active != active_scope(conn, round_queries.active_round(conn, ticket.source_id))
        or not set(request_ids) <= set(active.request_ids)
    ):
        return None
    return tuple(_binding(conn, config, claim, ticket, revision, request_id) for request_id in request_ids)


def _operation_scope(binding, claim, revision, request_id, request_ids, active, target, delivery):
    identity = binding.scope_digest if binding is not None else ["unbound", asdict(claim), revision, request_id]
    parts = [identity, request_ids]
    if active is not None:
        parts.append(asdict(active))
    if isinstance(delivery, RecoveredTapdScope):
        parts.append({"recovered_tapd": asdict(delivery)})
    elif isinstance(delivery, AcceptedRecoveryScope):
        parts.append({"recovery": asdict(delivery)})
    elif isinstance(delivery, AcceptedDispositionScope):
        parts.append({"disposition": asdict(delivery)})
    elif isinstance(delivery, AcceptedCompletionScope):
        parts.append({"completion": asdict(delivery)})
    elif delivery is not None:
        parts.append({"delivery": asdict(delivery)})
    if isinstance(target, ModelObservationTarget):
        parts.append({"model": asdict(target)})
    elif target is not None:
        parts.append(asdict(target))
    return context_digest(parts)


def _proof(operation_id, scope, evidence):
    payload = asdict(evidence)
    return {
        "scope_digest": scope,
        "status": evidence.status,
        "observed_at": evidence.observed_at,
        "evidence_json": json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
        "proof_digest": context_digest([operation_id, scope, payload]),
    }


def _unavailable(request_id, reason):
    return CurrentEvidence(request_id, "unavailable", reason, values.utc_now(), ItemRead("unavailable", reason))


def _observe(reader, binding, parents, request_id, delivery):
    target = binding.target
    coordinates = target["repository"], target["pr_number"]
    if coordinates not in parents:
        parents[coordinates] = reader.parent(*coordinates)
    parent = source_pr_selection.current_observation(binding.lineage, parents[coordinates], binding.lineage.author)
    if parent is None:
        return _unavailable(request_id, "github_parent_scope_unavailable")
    item = reader.item(
        **{field: target[field] for field in ("repository", "pr_number", "event_type", "item_id", "event_url")}
    )
    if item.status == "unavailable":
        return _unavailable(request_id, item.reason)
    if item.status == "absent":
        return CurrentEvidence(request_id, "absent", item.reason, values.utc_now(), item, parent=parent)
    if utc_timestamp(item.created_at) != utc_timestamp(binding.created_at) or utc_timestamp(
        item.updated_at
    ) < utc_timestamp(binding.updated_at):
        return _unavailable(request_id, "github_item_creation_identity_changed")
    actor = item.actor.casefold()
    trusted = {member.casefold() for member in binding.repo["trusted_actors"]}
    if item.author.casefold() != binding.creator.casefold():
        if delivery is None:
            return _unavailable(request_id, "github_item_creation_identity_changed")
        reason = "github_current_creator_changed"
    elif (
        actor not in trusted
        or item.author.casefold() not in trusted
        or item.author.casefold() == binding.repo["github_account"].casefold()
        or actor == binding.repo["github_account"].casefold()
        or item.author.casefold().endswith("[bot]")
        or actor.endswith("[bot]")
        or item.author_type == "Bot"
    ):
        reason = "github_current_actor_untrusted"
    elif not discovery.bot_requests(item.body, binding.repo["github_account"]):
        reason = "github_current_mention_removed"
    else:
        reason = "github_current_mention"
    version = source_members.github_content_version(target["event_id"], item.body, actor)
    return CurrentEvidence(request_id, "present", reason, values.utc_now(), item, version, parent)


def refresh_batch(
    db_path,
    config,
    claim,
    ticket,
    *,
    revision,
    request_ids,
    runner=None,
    stop=None,
    active=None,
    target=None,
    delivery=None,
):
    """Read exact events outside a writer and save a new bounded operation."""
    request_ids = tuple(request_ids)
    if not request_ids or len(request_ids) > BATCH_LIMIT or len(set(request_ids)) != len(request_ids):
        raise ValueError("GitHub source observation requires a nonempty bounded set of distinct request IDs")
    operation = values.new_id("source-github-observation")
    with _writer(db_path) as conn:
        if not source_sessions.lock_claim(conn, claim, now=values.utc_now()):
            return ObservationBatch(None, "source_claim_changed")
        bindings = _bindings(conn, config, claim, ticket, revision, request_ids, active, target, delivery)
        if bindings is None:
            return ObservationBatch(None, "github_observation_scope_changed")
        for request_id, binding in zip(request_ids, bindings, strict=True):
            proof = _proof(
                operation,
                _operation_scope(binding, claim, revision, request_id, request_ids, active, target, delivery),
                _unavailable(request_id, "github_observation_in_progress"),
            )
            if not write.save_current_observation(
                conn, request_id=request_id, operation_id=operation, fields=proof, initial=True
            ):
                raise ValueError("GitHub source observation reservation failed")

    reader = DiscussionReader(runner=runner, stop=stop)
    parents = {}
    evidence = tuple(
        _observe(reader, binding, parents, request_id, delivery)
        if binding is not None
        else _unavailable(request_id, "github_request_binding_unavailable")
        for request_id, binding in zip(request_ids, bindings, strict=True)
    )
    with _writer(db_path) as conn:
        if not source_sessions.lock_claim(conn, claim, now=values.utc_now()):
            return ObservationBatch(None, "source_claim_changed")
        if _bindings(conn, config, claim, ticket, revision, request_ids, active, target, delivery) != bindings or any(
            row is None or row["operation_id"] != operation
            for request_id in request_ids
            for row in (read.current_observation(conn, request_id),)
        ):
            return ObservationBatch(None, "github_observation_scope_changed")
        for observed, binding in zip(evidence, bindings, strict=True):
            proof = _proof(
                operation,
                _operation_scope(binding, claim, revision, observed.request_id, request_ids, active, target, delivery),
                observed,
            )
            if not write.save_current_observation(
                conn, request_id=observed.request_id, operation_id=operation, fields=proof, initial=False
            ):
                raise ValueError("GitHub source observation was superseded")
    return ObservationBatch(operation, "github_observation_recorded", evidence)


def current_batch(
    conn, config, claim, ticket, *, revision, operation_id, request_ids, now, active=None, target=None, delivery=None
):
    """In the caller's writer, fence the exact just-performed operation and current binding."""
    request_ids = tuple(request_ids)
    if (
        not operation_id
        or not request_ids
        or len(request_ids) > BATCH_LIMIT
        or len(set(request_ids)) != len(request_ids)
        or not source_sessions.lock_claim(conn, claim, now=now)
    ):
        return None
    bindings = _bindings(conn, config, claim, ticket, revision, request_ids, active, target, delivery)
    if bindings is None:
        return None
    result = []
    for request_id, binding in zip(request_ids, bindings, strict=True):
        row = read.current_observation(conn, request_id)
        scope = _operation_scope(binding, claim, revision, request_id, request_ids, active, target, delivery)
        if row is None or row["operation_id"] != operation_id or row["scope_digest"] != scope:
            return None
        payload = json_object(row["evidence_json"])
        if (
            not payload
            or payload.get("request_id") != request_id
            or payload.get("status") != row["status"]
            or payload.get("observed_at") != row["observed_at"]
            or payload.get("reason") == "github_observation_in_progress"
            or context_digest([operation_id, scope, payload]) != row["proof_digest"]
        ):
            return None
        try:
            result.append(CurrentEvidence(**{**payload, "item": ItemRead(**payload["item"])}))
        except (KeyError, TypeError):
            return None
    return tuple(result)


def reseal_current(
    conn, config, claim, ticket, *, revision, operation_id, request_ids, checked, disposition, active=None, target=None
):
    """Bind the observed facts to the verified writer's exact newly stored request."""
    row = read.current_observation(conn, checked.request_id)
    if (
        not conn.in_transaction
        or not revision_current(conn, ticket, revision)
        or row is None
        or row["operation_id"] != operation_id
        or row["proof_digest"] != context_digest([operation_id, row["scope_digest"], asdict(checked)])
        or json_object(row["evidence_json"]) != asdict(checked)
        or checked.request_id not in request_ids
    ):
        return False
    binding = _binding(conn, config, claim, ticket, revision, checked.request_id)
    scope = _operation_scope(binding, claim, revision, checked.request_id, request_ids, active, target, None)
    return write.save_current_observation(
        conn,
        request_id=checked.request_id,
        operation_id=operation_id,
        fields=_proof(operation_id, scope, replace(checked, disposition=disposition)),
        initial=False,
    )


def read_operations(
    db_path,
    config,
    claim,
    ticket,
    *,
    revision,
    request_ids,
    runner=None,
    stop=None,
    active=None,
    target=None,
    delivery=None,
):
    """Read every member in bounded operations with no network in a writer."""
    operations, evidence = [], []
    for start in range(0, len(request_ids), BATCH_LIMIT):
        ids = request_ids[start : start + BATCH_LIMIT]
        batch = refresh_batch(
            db_path,
            config,
            claim,
            ticket,
            revision=revision,
            request_ids=ids,
            runner=runner,
            stop=stop,
            active=active,
            target=target,
            delivery=delivery,
        )
        if batch.operation_id is None:
            return (), (), batch.reason
        operations.append((batch.operation_id, ids))
        evidence.extend(batch.evidence)
    reason = "github_observation_unavailable" if stop is not None and stop.is_set() else None
    return tuple(operations), tuple(evidence), reason
