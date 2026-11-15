"""Write exact source result, required action and handled member associations."""

import json

from sqlalchemy import func, insert, literal, select, update

from robert_agent.foundation.contracts.delivery import (
    AuditStatus,
    NATIVE_RESULT_CONTRACT,
    OutputType,
    PublicationStatus,
    PublishStatus,
    SourceControllerAction,
    SourceResultKind,
)
from robert_agent.foundation.contracts.execution import AttemptStatus, ModelRunStatus, SourceModelExit, SourceModelStage
from robert_agent.foundation.contracts.work import (
    RequestEventState,
    SourceIngressPhase,
    SourceRoundStatus,
    TaskKind,
    TaskLifecycle,
)
from robert_agent.persistence import statements, transactions
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.expressions import valid_json_or_empty
from robert_agent.persistence.queries import (
    result_contract,
    source_members,
    source_results as read,
    source_sessions,
    source_tasks,
)
from robert_agent.persistence.rounds import source_notice
from robert_agent.persistence.rounds.source_evidence import context_digest, json_object, source_model_blocker


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def insert_result(
    conn,
    *,
    source_result_id,
    source_id,
    round_id,
    kind,
    input_version,
    idempotency_key,
    evidence,
    payload,
    now,
    worker_result_id=None,
    model_run_id=None,
):
    if not conn.in_transaction:
        raise ValueError("Source result writes require a caller-owned transaction")
    table = tables["source_results"]
    statements.write(
        conn,
        insert(table).values(
            source_result_id=source_result_id,
            source_id=source_id,
            round_id=round_id,
            kind=kind,
            input_version=input_version,
            idempotency_key=idempotency_key,
            worker_result_id=worker_result_id,
            model_run_id=model_run_id,
            evidence_json=_json(evidence),
            payload_json=_json(payload),
            accepted_at=now,
        ),
    )


def attach_required_actions(conn, *, source_result_id, action_ids):
    link, root, action, member = (
        tables[name] for name in ("source_required_actions", "source_results", "github_actions", "source_round_tasks")
    )
    for position, action_id in enumerate(action_ids):
        exact = (
            select(root.c.source_result_id)
            .where(root.c.source_result_id == source_result_id)
            .where(
                select(action.c.action_id)
                .where(
                    action.c.action_id == action_id,
                    (action.c.source_result_id == root.c.source_result_id)
                    | (
                        (action.c.result_id == root.c.worker_result_id)
                        & action.c.task_id.in_(select(member.c.task_id).where(member.c.round_id == root.c.round_id))
                    ),
                )
                .correlate(root)
                .exists()
            )
        )
        if not statements.read(conn, exact):
            raise ValueError("Required action has a different durable source result owner")
        statements.write(
            conn,
            insert(link).values(
                source_result_id=source_result_id,
                action_id=action_id,
                position=position,
            ),
        )


def insert_source_comment(conn, *, source_result_id, action_id, target, metadata, now):
    """Record a root-owned comment candidate; publisher still owns current scope and effects."""
    action, root, source = (tables[name] for name in ("github_actions", "source_results", "sources"))
    # Bind the historical action to the exact real TAPD source.
    source_owner = (
        select(root.c.source_result_id)
        .select_from(root.join(source, source.c.source_id == root.c.source_id))
        .where(
            root.c.source_result_id == source_result_id,
            source.c.provider == "tapd",
            source.c.host == target["host"],
            source.c.project_key == target["workspace_id"],
            source.c.source_type == target["kind"],
            source.c.external_id == target["external_id"],
        )
    )
    changed = statements.write(
        conn,
        insert(action).from_select(
            (
                "action_id",
                "source_result_id",
                "action_type",
                "provider",
                "target_json",
                "audit_status",
                "metadata_json",
                "created_at",
            ),
            source_owner.with_only_columns(
                literal(action_id),
                root.c.source_result_id,
                literal("comment"),
                literal("tapd"),
                literal(_json(target)),
                literal("accepted"),
                literal(_json(metadata)),
                literal(now),
            ),
        ),
    )
    if changed != 1:
        raise ValueError("Source comment target differs from its real TAPD source")


def insert_source_feedback(conn, *, source_result_id, now):
    """Insert the exact root-derived controller suffix in the acceptance savepoint."""
    root = read.by_id(conn, source_result_id)
    planned = read.feedback_plan(conn, root) if root is not None else None
    if planned is None or not conn.in_transaction:
        raise ValueError("Cannot prove source GitHub feedback targets")
    action = tables["github_actions"]
    for entry in planned:
        statements.write(
            conn,
            insert(action).values(
                action_id=entry.action_id,
                source_result_id=source_result_id,
                action_type=SourceControllerAction.GITHUB_FEEDBACK.value,
                provider="github",
                target_json=_json(entry.target),
                audit_status=AuditStatus.ACCEPTED.value,
                metadata_json=_json(entry.metadata),
                created_at=now,
            ),
        )
    return tuple(entry.action_id for entry in planned)


def retire_fixed_notice(conn, *, source_id, source_result_id, observation_revision, now):
    """Retire an exact never-started ordinary notice under the caller source writer."""
    if not conn.in_transaction:
        raise ValueError("Notice retirement requires a caller-owned transaction")
    root = read.for_source(conn, source_id, source_result_id)
    source = read.tapd_source(conn, source_id) if root is not None else None
    candidate = read.fixed_notice_action(conn, source, root) if source is not None else None
    if candidate is None or not source_notice.never_started(candidate):
        return False
    action, intent = (tables[name] for name in ("github_actions", "publication_intents"))
    proof = source_notice.retirement_proof(candidate, observation_revision, now)
    metadata = json_object(candidate["metadata_json"])
    metadata[source_notice.RETIREMENT] = proof
    checkpoint = "retire_fixed_notice"
    transactions.savepoint(conn, checkpoint)
    try:
        if candidate["publication_id"] is not None:
            changed = statements.write(
                conn,
                update(intent)
                .where(
                    intent.c.publication_id == candidate["publication_id"],
                    intent.c.action_id == candidate["action_id"],
                    intent.c.status == PublicationStatus.PREPARED.value,
                    intent.c.started_at.is_(None),
                    intent.c.finished_at.is_(None),
                )
                .values(
                    status=PublicationStatus.FAILED.value, finished_at=now, updated_at=now, error_json=_json(proof)
                ),
            )
            if changed != 1:
                raise ValueError("Fixed notice intent changed before retirement")
        changed = statements.write(
            conn,
            update(action)
            .where(
                action.c.action_id == candidate["action_id"],
                action.c.source_result_id == source_result_id,
                action.c.publish_status == PublishStatus.NOT_PUBLISHED.value,
            )
            .values(publish_status=PublishStatus.SKIPPED.value, metadata_json=_json(metadata)),
        )
        if changed != 1:
            raise ValueError("Fixed notice action changed before retirement")
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.release(conn, checkpoint)
    return True


_WORKER_OUTPUTS = {
    SourceResultKind.ANSWER.value: {
        OutputType.COMMENT_ANALYSIS.value,
        OutputType.NEW_PR.value,
        OutputType.UPDATE_EXISTING_PR.value,
    },
    SourceResultKind.QUESTION.value: {OutputType.WAITING_FOR_USER.value},
}


def _worker_handled(conn, root, evidence, members):
    result = read.worker_candidate(conn, root["worker_result_id"])
    if result is None:
        return None
    binding = result_contract.task_round_binding(conn, result["task_id"])
    metadata = json_object(result["metadata_json"]) or {}
    audit = metadata.get("audit")
    if (
        binding is None
        or binding["round_id"] != root["round_id"]
        or binding["source_id"] != root["source_id"]
        or binding["request_id"] not in {row.member["request_id"] for row in members}
        or result["task_kind"] != TaskKind.HUMAN_REQUEST.value
        or result["attempt_status"] != AttemptStatus.COMPLETED.value
        or result["output_type"] not in _WORKER_OUTPUTS[root["kind"]]
        or evidence.get("attempt_id") != result["attempt_id"]
        or evidence.get("task_id") != result["task_id"]
        or evidence.get("repo_id") != binding["repo_id"]
        or metadata.get("result_contract") != NATIVE_RESULT_CONTRACT
        or metadata.get("submission_fingerprint") != evidence.get("submission_fingerprint")
        or not isinstance(metadata.get("submission_fingerprint"), str)
        or not isinstance(audit, dict)
        or audit.get("status") != AuditStatus.ACCEPTED.value
        or audit.get("ok") is not True
        or source_tasks.trigger_bindings(conn, result["task_id"])
        != {event["event_id"] for member in members for event in member.ownership.events}
    ):
        return None
    try:
        return json.loads(result["consumed_event_fingerprints_json"])
    except (TypeError, ValueError, RecursionError):
        return None


def _model_handled(conn, root, evidence, members):
    run = source_sessions.model_run(conn, root["model_run_id"])
    if run is None:
        return None
    output = json_object(run["output_json"])
    exit_proof = json_object(run["exit_json"])
    if (
        output is None
        or exit_proof is None
        or exit_proof.get("kind") != SourceModelExit.GROUP_EXITED.value
        or exit_proof.get("owner_token") != run["launch_token"]
        or source_model_blocker(run) is not None
        or run["round_id"] != root["round_id"]
        or run["request_id"] not in {row.member["request_id"] for row in members}
        or run["input_version"] != root["input_version"]
        or run["status"] != ModelRunStatus.SUCCEEDED.value
        or run["slot_reserved"]
        or (
            run["stage"] != SourceModelStage.SYNTHESIS.value
            and (run["stage"] != SourceModelStage.IDENTIFY.value or root["kind"] != SourceResultKind.QUESTION.value)
        )
        or output.get("kind") != root["kind"]
        or evidence.get("stage") != run["stage"]
        or evidence.get("prompt_sha256") != run["prompt_sha256"]
        or evidence.get("output_sha256") != context_digest(output)
    ):
        return None
    return output.get("handled_event_fingerprints")


def _handled_evidence(members, declared):
    if (
        not isinstance(declared, list)
        or not declared
        or any(not isinstance(value, str) or not value for value in declared)
        or len(set(declared)) != len(declared)
    ):
        return None, None
    wanted, available = set(declared), set()
    handled, targets = [], []
    for frozen in members:
        member, ownership = frozen.member, frozen.ownership
        snapshot = json_object(member["content_snapshot_json"])
        events = []
        if ownership.request["phase"] != SourceIngressPhase.PENDING.value:
            return None, None
        for event in ownership.events:
            fingerprint = event["event_fingerprint"]
            if (
                event["state"] != RequestEventState.PENDING.value
                or any(event[key] is not None for key in ("consumed_at", "source_result_id", "result_id"))
                or event["event_id"] != snapshot.get("event_id")
                or fingerprint in available
            ):
                return None, None
            available.add(fingerprint)
            events.append({"row_id": event["row_id"], "event_id": event["event_id"], "fingerprint": fingerprint})
        fingerprints = {event["fingerprint"] for event in events}
        if fingerprints & wanted:
            if not fingerprints <= wanted:
                return None, None
            handled.append(
                {"request_id": member["request_id"], "content_version": member["content_version"], "events": events}
            )
            if ownership.return_target is not None:
                targets.append({"request_id": member["request_id"], "target": ownership.return_target})
    return (handled, targets) if wanted <= available else (None, None)


def consume_accepted_members(conn, *, source_result_id, row_ids, now):
    """Consume exactly the accepted declaration's frozen ledger rows in the caller writer."""
    root = read.by_id(conn, source_result_id)
    round_ = read.delivery_round(conn, root["source_id"], root["round_id"]) if root else None
    evidence = json_object(root["evidence_json"]) if root else None
    if (
        not conn.in_transaction
        or round_ is None
        or root["kind"] not in _WORKER_OUTPUTS
        or round_["status"] != SourceRoundStatus.ACTIVE.value
        or root["input_version"] != round_["input_version"]
        or not evidence
        or evidence.get("round_input_sha256") != round_["input_sha256"]
    ):
        raise ValueError("Source result lacks exact frozen round ownership")
    members = source_members.frozen_members(conn, root["round_id"])
    declared = (
        _worker_handled(conn, root, evidence, members)
        if root["worker_result_id"] is not None
        else _model_handled(conn, root, evidence, members)
    )
    handled, targets = _handled_evidence(members, declared)
    row_ids = tuple(row_ids)
    if (
        not handled
        or evidence.get("handled_members") != handled
        or evidence.get("github_return_targets", []) != targets
        or any(type(row_id) is not int for row_id in row_ids)
        or len(row_ids) != len(set(row_ids))
        or {event["row_id"] for member in handled for event in member["events"]} != set(row_ids)
    ):
        raise ValueError("Source result differs from its declared frozen ledger or GitHub return targets")
    if targets and read.required_action_sets(conn, root, read.required_actions(conn, source_result_id)) is None:
        raise ValueError("Source result lacks required GitHub feedback obligations")
    ledger = tables["request_events"]
    checkpoint = "consume_accepted_source_members"
    transactions.savepoint(conn, checkpoint)
    try:
        count = statements.write(
            conn,
            update(ledger)
            .where(
                ledger.c.row_id.in_(row_ids),
                ledger.c.state == RequestEventState.PENDING.value,
                ledger.c.consumed_at.is_(None),
                ledger.c.result_id.is_(None),
                ledger.c.source_result_id.is_(None),
            )
            .values(state=RequestEventState.CONSUMED.value, consumed_at=now, source_result_id=source_result_id),
        )
        if count != len(row_ids):
            raise ValueError("Source result ledger members changed during acceptance")
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.release(conn, checkpoint)


def finish_worker_execution(conn, *, task_id, now):
    """Complete only the accepted native Worker; business completion belongs to receipts."""
    task = tables["tasks"]
    return (
        statements.write(
            conn,
            update(task)
            .where(
                task.c.task_id == task_id,
                task.c.task_kind == TaskKind.HUMAN_REQUEST.value,
                task.c.lifecycle == TaskLifecycle.RUNNING.value,
            )
            .values(lifecycle=TaskLifecycle.COMPLETED.value, updated_at=now),
        )
        == 1
    )


def insert_tapd_publication_preparation(conn, *, publication_id, evidence):
    """Store the controller's one immutable prepared comment beside its existing intent."""
    if not conn.in_transaction:
        raise ValueError("Source publication preparation requires a caller-owned transaction")
    prepared, intent, action = (
        tables[name] for name in ("tapd_publication_preparations", "publication_intents", "github_actions")
    )
    selected = (
        select(intent.c.publication_id, literal(_json(evidence)))
        .select_from(intent.join(action, action.c.action_id == intent.c.action_id))
        .where(
            intent.c.publication_id == publication_id,
            intent.c.status == "prepared",
            action.c.action_id == evidence["action_id"],
            *(
                func.json_extract(valid_json_or_empty(action.c.target_json), f"$.{key}") == evidence["target"][key]
                for key in ("provider", "host", "workspace_id", "kind", "external_id")
            ),
            action.c.provider == "tapd",
            action.c.action_type == "comment",
        )
    )
    if (
        statements.write(conn, insert(prepared).from_select(("publication_id", "prepared_evidence_json"), selected))
        != 1
    ):
        raise ValueError("Prepared comment does not match an existing TAPD intent")
