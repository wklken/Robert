"""Fenced taskless source model stages. The outer caller supplies current authority."""

from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import threading
import time
from uuid import uuid4

from robert_agent.adapters.processes import source_model as transport
from robert_agent.context import source_prompts
from robert_agent.execution import source_model_output
from robert_agent.foundation.contracts.delivery import AuditStatus, NATIVE_RESULT_CONTRACT, OutputType
from robert_agent.foundation.contracts.execution import (
    AttemptStatus,
    ModelCommandStatus,
    ModelRunStatus,
    SourceModelExit,
    SourceModelStage,
)
from robert_agent.foundation.contracts.work import SourceCapability, SourceRoundStatus, TaskKind, TaskLifecycle
from robert_agent.foundation.values import utc_now
from robert_agent.persistence.errors import Error as DatabaseError
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import source_sessions as read
from robert_agent.persistence.queries import (
    request_events,
    result_contract,
    source_members,
    source_results,
    worker_results,
)
from robert_agent.persistence.rounds import source_evidence, source_model as store
from robert_agent.persistence.stores import source_sessions
from robert_agent.security.redaction import redact_text
from robert_agent.work.application import source_model_observation, tapd_intake

LEASE_SECONDS = 30
RECHECK_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class SourceStageResult:
    status: str
    run_id: str
    reason: str | None = None


def select_worker(config):
    name = config["tapd"].get("model_worker") or (config.get("default_worker") or {}).get("name")
    selected = [worker for worker in config["workers"] if worker["name"] == name]
    if len(selected) != 1:
        raise transport.UnsupportedProfile("source_model_worker_unavailable")
    return transport.profile_for(selected[0])


@contextmanager
def _writer(db_path):
    with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
        transactions.begin_immediate(conn)
        yield conn


class _ClaimAwareStop:
    """Event-compatible cancellation that rechecks the claim in bounded transport polls."""

    def __init__(self, external, check):
        self.external = external
        self.check = check
        self.local = threading.Event()
        self.next_check = time.monotonic() + RECHECK_SECONDS

    def is_set(self):
        if self.local.is_set() or self.external.is_set():
            return True
        if time.monotonic() >= self.next_check:
            if not self.check():
                self.local.set()
            self.next_check = time.monotonic() + RECHECK_SECONDS
        return self.local.is_set()

    def wait(self, seconds):
        if not self.is_set():
            self.external.wait(seconds)
        return self.is_set()


def _allowed(conn, claim, round_id, request_id, stage, authorize):
    members = source_sessions.pending_round_members(conn, round_id)
    return any(member["request_id"] == request_id for member in members) and all(
        authorize(conn, claim, round_id, member["request_id"], stage) is True for member in members
    )


def _legacy_child_scope(conn, round_id, child_results, synthesis_ready):
    if synthesis_ready is None or not child_results:
        return False
    members = read.round_tasks(conn, round_id)
    try:
        by_task = {result["task_id"]: result for result in child_results}
    except (KeyError, TypeError):
        return False
    if len(by_task) != len(child_results) or set(by_task) != {row["task_id"] for row in members}:
        return False
    for member in members:
        result = by_task[member["task_id"]]
        if set(result) != {"task_id", "repo_id", "result_id", "result_sha256", "content"}:
            return False
        if result["repo_id"] != member["repo_id"] or not isinstance(result["content"], dict):
            return False
        content_digest = sha256(
            json.dumps(result["content"], ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
        if content_digest != result["result_sha256"]:
            return False
        stored = worker_results.find_for_results_for_task(conn, member["task_id"], 1)
        if not stored or stored[0]["result_id"] != result["result_id"]:
            return False
    return synthesis_ready(conn, round_id, tuple(dict(row) for row in members), child_results) is True


def verified_model_output(conn, round_record, run_id):
    """Read one v2 output and its committed inputs after exact durable exit proof."""
    run = read.model_run(conn, run_id)
    exit_proof = source_evidence.json_object(run["exit_json"]) if run else None
    if (
        run is None
        or run["round_id"] != round_record["round_id"]
        or run["input_version"] != round_record["input_version"]
        or run["status"] != ModelRunStatus.SUCCEEDED
        or exit_proof is None
        or exit_proof.get("kind") != SourceModelExit.GROUP_EXITED
        or exit_proof.get("owner_token") != run["launch_token"]
        or source_evidence.source_model_blocker(run) is not None
        or not isinstance(run["output_json"], str)
    ):
        return None
    packet = source_evidence.frozen_prompt_packet(run)
    if packet is None or packet["version"] != 2:
        return None
    context = packet["context"]
    try:
        candidates = json.loads(round_record["repo_ids_json"])
        if (
            context["stage"] != run["stage"]
            or context["input_sha256"] != round_record["input_sha256"]
            or context["candidates"] != candidates
            or len(packet["payload"].encode()) > source_prompts.MAX_INPUT_BYTES
            or context["request_members"] != request_events.frozen_model_members(conn, run["round_id"])
            or run["request_id"] not in {member["request_id"] for member in context["request_members"]}
        ):
            return None
        supplied = source_model_output.validate_evidence(context["evidence"], candidates)
        output = source_model_output.validate(
            run["output_json"].encode(),
            stage=run["stage"],
            binding={key: run[key] for key in ("round_id", "run_id", "input_version", "prompt_sha256")},
            candidates=candidates,
            supplied=supplied,
            members=context["request_members"],
        )
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        return None
    return run, context, output, supplied


def _selected_repos(conn, round_record):
    selections = set()
    for run in read.model_runs(conn, round_record["round_id"]):
        if run["stage"] != SourceModelStage.IDENTIFY or run["status"] != ModelRunStatus.SUCCEEDED:
            continue
        verified = verified_model_output(conn, round_record, run["run_id"])
        if verified is None or verified[2]["kind"] != "selection":
            return frozenset()
        selections.add(frozenset(verified[2]["repositories"]))
    if selections:
        return next(iter(selections)) if len(selections) == 1 else frozenset()
    frozen_repos = json.loads(round_record["repo_ids_json"])
    return frozenset(frozen_repos) if len(frozen_repos) == 1 else frozenset()


def _evidence_child(conn, round_record, member):
    task_id = member["task_id"]
    result_id = source_results.latest_worker_result_id(conn, task_id)
    result = source_results.worker_candidate(conn, result_id) if result_id else None
    binding = result_contract.task_round_binding(conn, task_id)
    if (
        result is None
        or binding is None
        or result["output_type"] != OutputType.LOCAL_RESULT
        or result["task_kind"] != TaskKind.HUMAN_REQUEST
        or result["lifecycle"] != TaskLifecycle.COMPLETED
        or result["attempt_status"] != AttemptStatus.COMPLETED
        or binding["round_id"] != round_record["round_id"]
        or binding["source_id"] != round_record["source_id"]
        or binding["repo_id"] != member["repo_id"]
        or binding["capability"] != SourceCapability.READONLY
        or not read.attempt_is_settled(
            conn, task_id, result["attempt_id"], subject_type="task", subject_id=task_id, require_exit=True
        )
        or source_results.candidate_actions(conn, result_id)
    ):
        return None
    metadata = source_evidence.json_object(result["metadata_json"]) or {}
    audit = metadata.get("audit")
    normalized = metadata.get("normalized_payload")
    source = normalized.get("native_source") if isinstance(normalized, dict) else None
    if (
        metadata.get("result_contract") != NATIVE_RESULT_CONTRACT
        or not isinstance(audit, dict)
        or audit.get("status") != AuditStatus.ACCEPTED
        or audit.get("ok") is not True
        or audit.get("planned_actions") != []
        or not isinstance(source, dict)
        or normalized.get("planned_actions") not in (None, [])
        or source.get("role") != "evidence"
        or source.get("round_input_sha256") != round_record["input_sha256"]
        or any(
            source.get(key) != binding[key] for key in ("round_id", "request_id", "source_id", "repo_id", "capability")
        )
    ):
        return None
    refs = normalized.get("evidence", [])
    question = normalized.get("investigation_question")
    handoff = result["handoff"]
    if (
        not isinstance(refs, list)
        or len(refs) > 32
        or (bool(refs) and question is not None)
        or (not refs and (not isinstance(question, str) or not question.strip() or len(question) > 700))
    ):
        return None
    try:
        if any(source_model_output.reference(ref)[0] != member["repo_id"] for ref in refs):
            return None
    except (ValueError, TypeError):
        return None
    if any(redact_text(text, strict_assignments=True).get("text") != text for text in (handoff, question or "")):
        return None
    content = {"handoff": handoff, "references": refs}
    if question is not None:
        content["investigation_question"] = question
    return {
        "task_id": task_id,
        "repo_id": member["repo_id"],
        "result_id": result_id,
        "result_sha256": source_evidence.context_digest(content),
        "content": content,
    }


def synthesis_children(conn, round_record):
    """Derive one accepted, exited evidence child for every durably selected repository."""
    if round_record["capability"] != SourceCapability.READONLY:
        return ()
    selected = _selected_repos(conn, round_record)
    children = []
    for member in read.round_tasks(conn, round_record["round_id"]):
        child = _evidence_child(conn, round_record, member)
        if child is None:
            return ()
        children.append(child)
    if not selected or len(children) != len(selected) or {child["repo_id"] for child in children} != selected:
        return ()
    return tuple(children)


def synthesis_evidence_matches(conn, round_record, children, supplied):
    verified = synthesis_children(conn, round_record)
    return (
        bool(verified)
        and tuple(children) == verified
        and supplied
        == {source_model_output.reference(ref) for child in verified for ref in child["content"]["references"]}
    )


def _profile_key(profile):
    return {
        "worker_id": profile.worker_id,
        "executable": str(profile.executable),
        "model": profile.model,
        "effort": profile.effort,
        "timeout": profile.timeout,
        "credential_names": list(profile.credential_names),
    }


def _packet(round_record, stage, run_id, profile, evidence, omissions, child_results, members):
    candidates = tuple(json.loads(round_record["repo_ids_json"]))
    snapshot = json.loads(round_record["input_snapshot_json"])
    records = tuple(asdict(record) for record in evidence)
    source_model_output.validate_evidence(records, candidates)
    context = {
        "stage": stage,
        "request_members": members,
        "evidence": records,
        "candidates": candidates,
        "child_results": child_results,
        "input_sha256": round_record["input_sha256"],
    }
    prompt = source_prompts.build(
        stage=stage,
        round_id=round_record["round_id"],
        run_id=run_id,
        input_version=round_record["input_version"],
        snapshot=snapshot,
        candidates=candidates,
        evidence=records,
        omissions=tuple(asdict(record) for record in omissions),
        child_results=child_results,
        members=members,
        context_sha256=source_evidence.context_digest(context),
    )
    packet = {"version": 2, "payload": prompt.payload, "context": context, "profile": _profile_key(profile)}
    return packet, prompt.binding


def _saved_packet(row, active, profile):
    packet = source_evidence.frozen_prompt_packet(row)
    context = _packet_context(packet) if packet else {}
    if (
        packet is None
        or packet["profile"] != _profile_key(profile)
        or context["input_sha256"] != active["input_sha256"]
        or context["candidates"] != json.loads(active["repo_ids_json"])
        or (packet["version"] == 2 and context["stage"] != row["stage"])
        or len(packet["payload"].encode()) > source_prompts.MAX_INPUT_BYTES
    ):
        raise ValueError("source model frozen input scope changed")
    binding = {key: row[key] for key in ("round_id", "run_id", "input_version", "prompt_sha256")}
    return packet, binding


def _packet_context(packet):
    return packet["context"] if packet["version"] == 2 else packet


def _supplied(packet, context, candidates):
    if packet["version"] == 2:
        return source_model_output.validate_evidence(context["evidence"], candidates)
    return {
        source_model_output.reference(dict(zip(("repo_id", "path", "revision"), row, strict=True)))
        for row in packet["references"]
    }


def execute(
    db_path,
    claim,
    *,
    round_id,
    request_id,
    run_id,
    stage,
    profile,
    evidence=(),
    omissions=(),
    child_results=(),
    authorize,
    synthesis_ready=None,
    mixed_observer=None,
    stop=None,
    runner=transport.run,
    now=utc_now,
):
    """Run one stage after persisting its slot and launch grant in a short writer."""
    if stage not in (SourceModelStage.IDENTIFY, SourceModelStage.SYNTHESIS):
        raise ValueError("invalid source model stage")
    if not isinstance(profile, transport.SourceModelProfile):
        return SourceStageResult("attention", run_id, "unsupported_source_model_profile")
    external = stop if stop is not None else threading.Event()
    if external.is_set():
        return SourceStageResult("interrupted", run_id, "stopped_before_reservation")
    with closing(database.connect(db_path, foreign_keys=True)) as conn:
        active = read.active_round(conn, claim.source_id)
        if active is None or active["round_id"] != round_id or active["capability"] != SourceCapability.READONLY:
            return SourceStageResult("denied", run_id, "readonly_round_required")
        prior = read.model_run(conn, run_id)
        members = request_events.frozen_model_members(conn, round_id)
        mixed = bool(source_members.round_github_request_ids(conn, round_id))
    if mixed and not isinstance(mixed_observer, source_model_observation.MixedModelObserver):
        return SourceStageResult("denied", run_id, "source_model_authority_unavailable")
    try:
        if prior is None:
            if not members:
                return SourceStageResult("denied", run_id, "source_model_members_unavailable")
            packet, binding = _packet(active, stage, run_id, profile, evidence, omissions, child_results, members)
        elif (
            prior["round_id"] != round_id
            or prior["request_id"] != request_id
            or prior["stage"] != stage
            or prior["worker_id"] != profile.worker_id
        ):
            return SourceStageResult("denied", run_id, "source_model_run_scope_changed")
        elif prior["input_packet_json"] is None:
            return SourceStageResult("attention", run_id, "frozen_model_input_unavailable")
        else:
            packet, binding = _saved_packet(prior, active, profile)
        context = _packet_context(packet)
        child_results = tuple(context["child_results"])
        candidates = tuple(context["candidates"])
        supplied = _supplied(packet, context, candidates)
        frozen_members = context.get("request_members")
        if any(repo_id not in candidates for repo_id, _path, _revision in supplied):
            return SourceStageResult("attention", run_id, "invalid_source_model_input")
    except source_prompts.SourcePromptCapacity:
        return SourceStageResult("attention", run_id, "required_source_context_exceeds_capacity")
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return SourceStageResult("attention", run_id, "invalid_source_model_input")

    def input_scope(conn):
        if not read.repositories_available(conn, candidates):
            return False
        if frozen_members is not None and (
            not source_sessions.pending_round_members(conn, round_id)
            or frozen_members != request_events.frozen_model_members(conn, round_id)
        ):
            return False
        if stage == SourceModelStage.IDENTIFY:
            return not child_results
        if packet["version"] == 1:
            return _legacy_child_scope(conn, round_id, child_results, synthesis_ready)
        return synthesis_evidence_matches(conn, active, child_results, supplied)

    launch_id = "source-launch-" + uuid4().hex
    run_deadline = None

    def observe(check, run):
        if not mixed:
            return None, None
        target = source_model_observation.check_target(
            active, binding, run, request_id=request_id, stage=stage, launch_id=launch_id, check=check
        )
        try:
            current_proof = mixed_observer.observe(target, external=external, deadline=run_deadline)
        except (DatabaseError, OSError, ValueError):
            current_proof = None
        return target, current_proof

    def launch_observation(check):
        if not mixed:
            return None, None
        try:
            with closing(database.connect(db_path)) as conn:
                run = read.model_run(conn, run_id)
        except (DatabaseError, OSError):
            return None, None
        if run is None:
            return None, None
        if check == "periodic" and run["status"] == ModelRunStatus.STARTING:
            return observe("pre_identity", run)
        return observe(check, run) if run["status"] == ModelRunStatus.RUNNING else (None, None)

    def authorized(conn, current, checked, timestamp):
        if not mixed:
            return _allowed(conn, claim, round_id, request_id, stage, authorize)
        target, proof = checked
        try:
            return mixed_observer.authorized(conn, claim, current, target, proof, now=timestamp)
        except (DatabaseError, OSError, ValueError):
            return False

    checked = observe("initial", prior)
    with _writer(db_path) as conn:
        timestamp = now()
        current = source_sessions.lock_round(conn, claim, round_id, now=timestamp)
        if (
            current is None
            or current["input_sha256"] != active["input_sha256"]
            or current["input_version"] != binding["input_version"]
            or (mixed and external.is_set())
            or not authorized(conn, current, checked, timestamp)
            or not input_scope(conn)
        ):
            return SourceStageResult("denied", run_id, "source_model_authority_unavailable")
        if not mixed:
            freshness = tapd_intake.round_input_status(conn, current)
            if freshness not in {"current", "frozen"}:
                reason = (
                    "source_model_input_changed" if freshness == "changed" else "source_model_authority_unavailable"
                )
                return SourceStageResult("denied", run_id, reason)
        latest = read.model_run(conn, run_id)
        if (latest is None) != (prior is None) or (
            latest is not None and latest["input_packet_json"] != prior["input_packet_json"]
        ):
            return SourceStageResult("waiting", run_id, "source_model_input_changed")
        row = store.enqueue(
            conn,
            claim,
            run_id=run_id,
            round_id=round_id,
            request_id=request_id,
            stage=stage,
            worker_id=profile.worker_id,
            input_version=binding["input_version"],
            prompt_sha256=binding["prompt_sha256"],
            input_packet=packet,
            now=timestamp,
        )
        if row is None:
            return SourceStageResult("waiting", run_id, "source_round_busy")
        if row["launch_id"] is not None or row["status"] not in (ModelRunStatus.WAITING, ModelRunStatus.STARTING):
            return SourceStageResult("existing", run_id, row["status"])
        if row["status"] == ModelRunStatus.WAITING and not store.reserve_slot(conn, claim, run_id, now=timestamp):
            return SourceStageResult("waiting", run_id, "model_capacity")
        if not source_sessions.renew(conn, claim, now=timestamp, lease_seconds=LEASE_SECONDS):
            # An already longer caller-owned lease is sufficient; we never shorten it.
            if not source_sessions.lock_claim(conn, claim, now=timestamp):
                return SourceStageResult("denied", run_id, "source_claim_changed")
        if not store.authorize_launch(conn, claim, run_id, launch_id=launch_id, now=timestamp):
            return SourceStageResult("denied", run_id, "source_launch_not_granted")

    run_deadline = time.monotonic() + profile.timeout if mixed else None

    def check_claim():
        try:
            checked = launch_observation("periodic")
            with _writer(db_path) as conn:
                timestamp = now()
                current = source_sessions.lock_round(conn, claim, round_id, now=timestamp)
                if (
                    current is None
                    or (mixed and external.is_set())
                    or not authorized(conn, current, checked, timestamp)
                    or not input_scope(conn)
                ):
                    return False
                source_sessions.renew(conn, claim, now=timestamp, lease_seconds=LEASE_SECONDS)
                return True
        except (DatabaseError, OSError, ValueError):
            return False

    gate = _ClaimAwareStop(external, check_claim)

    def on_started(identity):
        with _writer(db_path) as conn:
            timestamp = now()
            recorded = store.record_identity(conn, claim, run_id, launch_id=launch_id, identity=identity, now=timestamp)
            if not mixed:
                return (
                    recorded
                    and not external.is_set()
                    and _allowed(conn, claim, round_id, request_id, stage, authorize)
                    and input_scope(conn)
                )
        # Process identity remains recoverable even when the fresh read fails.
        checked = launch_observation("started") if recorded else (None, None)
        try:
            with _writer(db_path) as conn:
                timestamp = now()
                current = source_sessions.lock_round(conn, claim, round_id, now=timestamp)
                permitted = (
                    recorded
                    and current is not None
                    and not external.is_set()
                    and not gate.local.is_set()
                    and authorized(conn, current, checked, timestamp)
                    and input_scope(conn)
                )
        except (DatabaseError, OSError, ValueError):
            permitted = False
        if not permitted:
            gate.local.set()
        return permitted

    try:
        result = runner(profile, packet["payload"], gate, on_started)
    except (DatabaseError, OSError, ValueError, RuntimeError):
        return SourceStageResult("attention", run_id, "source_model_transport_unconfirmed")
    if result.exit_kind is None:
        return SourceStageResult("attention", run_id, "source_model_exit_unconfirmed")

    output = None
    reason = result.status
    if result.status == ModelCommandStatus.READ and result.exit_kind == SourceModelExit.GROUP_EXITED:
        try:
            output = source_model_output.validate(
                result.output,
                stage=stage,
                binding=binding,
                candidates=candidates,
                supplied=supplied,
                members=frozen_members,
            )
            if output["kind"] == "answer" and any(
                "investigation_question" in child["content"] for child in child_results
            ):
                raise ValueError("incomplete synthesis child evidence")
        except (ValueError, UnicodeError, RecursionError):
            output = None
            reason = ModelCommandStatus.INVALID_OUTPUT.value
    checked = launch_observation("final")
    with _writer(db_path) as conn:
        timestamp = now()
        current = source_sessions.lock_round(conn, claim, round_id, now=timestamp)
        if current is None:
            return SourceStageResult("attention", run_id, "source_claim_changed")
        allowed = (
            not external.is_set()
            and not gate.local.is_set()
            and authorized(conn, current, checked, timestamp)
            and input_scope(conn)
        )
        if not allowed or result.status == ModelCommandStatus.STOPPED:
            output, outcome = None, ModelRunStatus.INTERRUPTED.value
            reason = "source_model_cancelled"
        else:
            outcome = ModelRunStatus.SUCCEEDED.value if output is not None else ModelRunStatus.FAILED.value
        finished = store.finish(
            conn,
            claim,
            run_id,
            launch_id=launch_id,
            exit_kind=result.exit_kind,
            identity=result.identity,
            outcome=outcome,
            output=output,
            error=None if output else reason,
            now=timestamp,
        )
    return (
        SourceStageResult(outcome, run_id, None if output else reason)
        if finished
        else SourceStageResult("attention", run_id, "source_model_exit_unconfirmed")
    )


def recover(db_path, claim, *, run_id, inspect_exit=transport.group_exited, now=utc_now):
    """Reconcile an interrupted launch after takeover using exact recorded group evidence."""
    with closing(database.connect(db_path, foreign_keys=True)) as conn:
        row = read.model_run(conn, run_id)
        round_record = read.active_round(conn, claim.source_id)
    if (
        row is None
        or round_record is None
        or round_record["round_id"] != row["round_id"]
        or round_record["status"] != SourceRoundStatus.ACTIVE
    ):
        return SourceStageResult("denied", run_id, "source_model_scope_changed")
    if row["status"] not in (ModelRunStatus.STARTING, ModelRunStatus.RUNNING):
        return SourceStageResult("existing", run_id, row["status"])
    launch = row["launch_id"]
    identity = source_evidence.model_identity_facts(source_evidence.json_object(row["dispatch_json"]))
    if launch is not None and (identity is None or not inspect_exit(identity)):
        return SourceStageResult("attention", run_id, "source_model_exit_unconfirmed")
    if launch is None and row["dispatch_json"] is not None:
        return SourceStageResult("attention", run_id, "source_model_exit_unconfirmed")
    with _writer(db_path) as conn:
        latest = read.model_run(conn, run_id)
        if (
            latest is None
            or latest["launch_id"] != launch
            or latest["dispatch_json"] != row["dispatch_json"]
            or not store.finish(
                conn,
                claim,
                run_id,
                launch_id=launch,
                exit_kind=SourceModelExit.GROUP_EXITED.value if launch else None,
                identity=identity,
                outcome=ModelRunStatus.INTERRUPTED.value,
                error="source_model_reconciled",
                now=now(),
            )
        ):
            return SourceStageResult("attention", run_id, "source_model_recovery_changed")
    return SourceStageResult(ModelRunStatus.INTERRUPTED.value, run_id, "source_model_reconciled")


def cancel_recorded(db_path, claim, *, run_id, terminate=None, now=utc_now):
    """Stop an exact canceled source model outside SQLite, then record proven group exit."""
    from robert_agent.adapters.processes.identity import terminate_recorded_identity
    from robert_agent.persistence.queries.tapd_intake import round_cancellation

    terminate = terminate or terminate_recorded_identity
    with _writer(db_path) as conn:
        row = read.model_run(conn, run_id)
        if (
            row is None
            or source_sessions.lock_round(conn, claim, row["round_id"], now=now()) is None
            or round_cancellation(conn, row["round_id"]) is None
        ):
            return SourceStageResult("denied", run_id, "source_model_cancellation_unavailable")
        if row["status"] not in (ModelRunStatus.WAITING, ModelRunStatus.STARTING, ModelRunStatus.RUNNING):
            return SourceStageResult("existing", run_id, row["status"])
        launch = row["launch_id"]
        identity = source_evidence.model_identity_facts(source_evidence.json_object(row["dispatch_json"]))
        if launch is None:
            if store.finish(conn, claim, run_id, outcome=ModelRunStatus.INTERRUPTED, now=now()):
                return SourceStageResult("interrupted", run_id, "source_model_cancelled")
            return SourceStageResult("attention", run_id, "source_model_exit_unconfirmed")
        if identity is None:
            return SourceStageResult("attention", run_id, "source_model_identity_unconfirmed")
    try:
        outcome = terminate(identity)
    except (OSError, ValueError, RuntimeError):
        return SourceStageResult("attention", run_id, "source_model_exit_unconfirmed")
    if outcome.get("status") not in {"terminated", "already_dead"}:
        return SourceStageResult("attention", run_id, "source_model_exit_unconfirmed")
    with _writer(db_path) as conn:
        latest = read.model_run(conn, run_id)
        if (
            latest == row
            and round_cancellation(conn, row["round_id"]) is not None
            and store.finish(
                conn,
                claim,
                run_id,
                launch_id=launch,
                exit_kind=SourceModelExit.GROUP_EXITED,
                identity=identity,
                outcome=ModelRunStatus.INTERRUPTED,
                now=now(),
                error="source_model_cancelled",
            )
        ):
            return SourceStageResult("interrupted", run_id, "source_model_cancelled")
    return SourceStageResult("attention", run_id, "source_model_cancellation_changed")
