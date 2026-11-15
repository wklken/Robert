"""Materialize source round associations and persisted release blockers."""

from dataclasses import dataclass

from sqlalchemy import and_, func, or_, select

from robert_agent.foundation.contracts.delivery import (
    AuditStatus,
    PublicationStatus,
    PublishStatus,
    SourceControllerAction,
)
from robert_agent.foundation.contracts.execution import (
    RUNNING_ATTEMPT_STATUSES,
    AttemptStatus,
    ModelRunStatus,
)
from robert_agent.foundation.contracts.work import TERMINAL_TASK_LIFECYCLES, SourceRoundStatus, TaskKind, TaskLifecycle
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.expressions import valid_json_or_empty
from robert_agent.persistence.queries import (
    source_delivery_pauses,
    source_delivery_recovery,
    source_feedback,
    source_github_publication,
    source_results,
    source_retirement,
)
from robert_agent.persistence.rounds import source_notice
from robert_agent.persistence.rounds.source_evidence import json_object, source_model_blocker


@dataclass(frozen=True, slots=True)
class RoundBlocker:
    kind: str
    identity: str
    reason: str


def session(conn, source_id):
    table = tables["source_sessions"]
    return records.first(statements.read(conn, select(table).where(table.c.source_id == source_id)))


def active_round(conn, source_id):
    table = tables["source_rounds"]
    return records.first(
        statements.read(
            conn,
            select(table).where(table.c.source_id == source_id, table.c.status == SourceRoundStatus.ACTIVE.value),
        )
    )


def round_requests(conn, round_id):
    member = tables["source_round_requests"]
    return statements.read(conn, select(member).where(member.c.round_id == round_id).order_by(member.c.request_id))


def round_tasks(conn, round_id):
    member = tables["source_round_tasks"]
    return statements.read(
        conn, select(member).where(member.c.round_id == round_id).order_by(member.c.created_at, member.c.task_id)
    )


def model_run(conn, run_id):
    run = tables["source_model_runs"]
    return records.first(statements.read(conn, select(run).where(run.c.run_id == run_id)))


def model_runs(conn, round_id):
    run = tables["source_model_runs"]
    return statements.read(conn, select(run).where(run.c.round_id == round_id).order_by(run.c.created_at, run.c.run_id))


def _attempt_blocker(row, *, require_exit=False):
    metadata = json_object(row["metadata_json"])
    if metadata is None:
        return "attempt_evidence_invalid"
    dispatch, termination = metadata.get("dispatch"), metadata.get("termination")
    if (dispatch is not None and not isinstance(dispatch, dict)) or (
        termination is not None and not isinstance(termination, dict)
    ):
        return "attempt_evidence_invalid"
    dispatch = dispatch or {}
    termination = termination or {}
    cleanup = dispatch.get("cleanup_pending")
    pid = dispatch.get("pid")
    stopped_at = metadata.get("process_stopped_at")
    if (
        (cleanup is not None and type(cleanup) is not bool)
        or (pid is not None and (type(pid) is not int or pid < 1))
        or (stopped_at is not None and utc_timestamp(stopped_at) is None)
        or (termination and not isinstance(termination.get("status"), str))
    ):
        return "attempt_evidence_invalid"
    if row["status"] in RUNNING_ATTEMPT_STATUSES:
        return "attempt_running"
    if row["status"] not in {value.value for value in AttemptStatus}:
        return "attempt_evidence_invalid"
    if (
        cleanup is True
        and not stopped_at
        and termination.get("status") not in {"terminated", "not_found", "already_dead"}
    ):
        return "attempt_exit_unconfirmed"
    if stopped_at or termination.get("status") in {"terminated", "not_found", "already_dead"} or cleanup is False:
        return None
    if (
        require_exit
        or pid is not None
        or metadata.get("worker_started_at") is not None
        or dispatch.get("process_exited") is not None
        or dispatch.get("status") == "running"
        or any(
            dispatch.get(field) is not None
            for field in ("process_group_id", "process_start_token", "command_fingerprint")
        )
        or termination
    ):
        return "attempt_exit_unconfirmed"
    return None


def attempt_is_settled(conn, task_id, attempt_id, *, subject_type, subject_id, require_exit=False):
    """Result arrival alone does not prove that a launched Worker stopped."""
    attempt = tables["attempts"]
    row = records.first(
        statements.read(
            conn,
            select(attempt.c.status, attempt.c.metadata_json).where(
                attempt.c.attempt_id == attempt_id, attempt.c.task_id == task_id
            ),
        )
    )
    if row is None or _attempt_blocker(row, require_exit=require_exit):
        return False
    control = tables["execution_subjects"]
    subject = records.first(
        statements.read(
            conn,
            select(control.c.metadata_json).where(
                control.c.subject_type == subject_type, control.c.subject_id == subject_id
            ),
        )
    )
    metadata = json_object(subject[0]) if subject else None
    return metadata is not None and metadata.get("dispatch_inflight") is None


def _worker_blockers(conn, rounds):
    member, task, attempt, control = (
        tables[name] for name in ("source_round_tasks", "tasks", "attempts", "execution_subjects")
    )
    task_scope = member.join(task, task.c.task_id == member.c.task_id)
    for row in statements.read(
        conn,
        select(task.c.task_id, task.c.lifecycle, task.c.metadata_json)
        .select_from(task_scope)
        .where(member.c.round_id.in_(rounds))
        .order_by(task.c.task_id),
    ):
        if json_object(row["metadata_json"]) is None:
            yield RoundBlocker("task", row["task_id"], "task_evidence_invalid")
        elif row["lifecycle"] not in TERMINAL_TASK_LIFECYCLES | {TaskLifecycle.WAITING_FOR_USER.value}:
            yield RoundBlocker("task", row["task_id"], "task_runnable")
    for row in statements.read(
        conn,
        select(attempt)
        .select_from(member.join(attempt, attempt.c.task_id == member.c.task_id))
        .where(member.c.round_id.in_(rounds))
        .order_by(attempt.c.attempt_id),
    ):
        reason = _attempt_blocker(row)
        if reason:
            yield RoundBlocker("attempt", row["attempt_id"], reason)
    # The dispatcher checkpoints the launch intention under a task subject
    # before Popen, including before metadata has reached the attempt itself.
    for row in statements.read(
        conn,
        select(member.c.task_id, control.c.metadata_json)
        .select_from(
            task_scope.join(
                control,
                or_(
                    and_(control.c.subject_type == "task", control.c.subject_id == member.c.task_id),
                    and_(
                        control.c.subject_type == "remediation_episode",
                        control.c.subject_id
                        == func.json_extract(valid_json_or_empty(task.c.metadata_json), "$.remediation_episode_id"),
                    ),
                ),
            )
        )
        .where(member.c.round_id.in_(rounds))
        .order_by(control.c.subject_id),
    ):
        metadata = json_object(row["metadata_json"])
        if metadata is None or metadata.get("dispatch_inflight") is not None:
            yield RoundBlocker("launch", row["task_id"], "worker_launch_unconfirmed")


def _child_model_blockers(conn, rounds):
    member = tables["source_round_tasks"]
    for kind in ("review", "vote"):
        child = tables[f"{kind}_sessions"]
        run = tables[f"{kind}_runs"]
        scope = member.join(child, child.c.task_id == member.c.task_id).join(run, run.c.round_id == child.c.round_id)
        for row in statements.read(
            conn, select(run).select_from(scope).where(member.c.round_id.in_(rounds)).order_by(run.c.run_id)
        ):
            dispatch = {} if row["dispatch_json"] is None else json_object(row["dispatch_json"])
            if (
                row["slot_reserved"]
                or row["status"] in (ModelRunStatus.STARTING, ModelRunStatus.RUNNING)
                or dispatch is None
                or dispatch.get("cleanup_pending") is True
            ):
                yield RoundBlocker("model", row["run_id"], "child_model_cleanup_unconfirmed")
    run = tables["source_model_runs"]
    for row in statements.read(conn, select(run).where(run.c.round_id.in_(rounds)).order_by(run.c.run_id)):
        reason = source_model_blocker(row)
        if reason:
            yield RoundBlocker("model", row["run_id"], reason)


def _publication_blockers(conn, rounds, source_id, *, publish_root_id=None, started_effect=None):
    member, action, intent = (tables[name] for name in ("source_round_tasks", "github_actions", "publication_intents"))
    scope = member.join(action, action.c.task_id == member.c.task_id).outerjoin(
        intent, intent.c.action_id == action.c.action_id
    )
    waiting = intent.c.status.in_(
        (PublicationStatus.PREPARED.value, PublicationStatus.EXECUTING.value, PublicationStatus.ATTENTION.value)
    )
    unaudited_or_pending = and_(
        action.c.audit_status.in_((AuditStatus.PENDING.value, AuditStatus.ACCEPTED.value)),
        action.c.publish_status == PublishStatus.NOT_PUBLISHED.value,
    )
    root, required, source, prepared = (
        tables[name]
        for name in ("source_results", "source_required_actions", "sources", "tapd_publication_preparations")
    )
    root_scope = root.c.round_id.in_(rounds) if source_id is None else root.c.source_id == source_id
    root_effect = (
        select(required.c.action_id)
        .select_from(required.join(root, root.c.source_result_id == required.c.source_result_id))
        .where(root_scope)
    )
    claimed_published = and_(
        or_(action.c.provider == "tapd", and_(action.c.provider == "github", action.c.target_json.is_not(None))),
        or_(
            action.c.publish_status == PublishStatus.PUBLISHED.value,
            intent.c.status == PublicationStatus.PUBLISHED.value,
        ),
    )
    task_effect = (
        select(action.c.action_id)
        .select_from(scope)
        .where(member.c.round_id.in_(rounds), or_(waiting, unaudited_or_pending, claimed_published))
    )
    direct_effect = select(action.c.action_id).where(
        action.c.source_result_id.in_(select(root.c.source_result_id).where(root_scope))
    )
    scopes = (
        action.outerjoin(intent, intent.c.action_id == action.c.action_id)
        .outerjoin(required, required.c.action_id == action.c.action_id)
        .outerjoin(
            root, root.c.source_result_id == func.coalesce(required.c.source_result_id, action.c.source_result_id)
        )
        .outerjoin(source, source.c.source_id == root.c.source_id)
        .outerjoin(prepared, prepared.c.publication_id == intent.c.publication_id)
    )
    dormant = {}
    retired = {}
    for pause in source_delivery_pauses.latest_in_scope(conn, rounds, source_id):
        root_id = pause["source_result_id"]
        retired[root_id] = source_retirement.is_retired(conn, root_id)
        if retired[root_id]:
            continue
        owner = source_delivery_recovery.for_generation(conn, root_id, pause["generation"])
        if owner is not None:
            if source_delivery_recovery.applied_bound(conn, owner) is None:
                yield RoundBlocker("publication", root_id, "delivery_recovery_evidence_changed")
            elif owner["completed_at"] is None and root_id != publish_root_id:
                yield RoundBlocker("delivery", root_id, "delivery_recovery_active")
            elif owner["completed_at"] is not None:
                source = source_results.tapd_source(conn, pause["source_id"])
                if source is None or not source_results.confirmed_required_effects(
                    conn, root_id, source_results.source_target(source)
                ):
                    yield RoundBlocker("publication", root_id, "delivery_recovery_receipts_changed")
            continue
        effects = source_delivery_pauses.dormant_actions(conn, pause)
        if effects is None:
            yield RoundBlocker("publication", pause["source_result_id"], "delivery_pause_evidence_changed")
        else:
            dormant[pause["source_result_id"]] = frozenset(effects)
    for row in statements.read(
        conn,
        select(
            action,
            intent.c.publication_id,
            intent.c.status.label("intent_status"),
            intent.c.remote_evidence_json,
            intent.c.started_at,
            intent.c.finished_at,
            required.c.source_result_id.label("required_root_id"),
            root.c.idempotency_key.label("root_key"),
            root.c.kind.label("root_kind"),
            root.c.source_id.label("root_source_id"),
            prepared.c.prepared_evidence_json,
            source.c.host,
            source.c.project_key,
            source.c.source_type,
            source.c.external_id.label("source_external_id"),
            action.c.action_id.in_(root_effect).label("required_effect"),
        )
        .select_from(scopes)
        .where(
            or_(
                action.c.action_id.in_(task_effect),
                action.c.action_id.in_(root_effect),
                and_(action.c.action_id.in_(direct_effect), or_(waiting, unaudited_or_pending, claimed_published)),
            )
        )
        .order_by(action.c.action_id),
    ):
        root_id = row["required_root_id"]
        if row["required_effect"] and row["root_kind"] in {"answer", "question"}:
            if root_id not in retired:
                retired[root_id] = source_retirement.is_retired(conn, root_id)
            if retired[root_id]:
                continue
        if row["action_id"] in dormant.get(root_id, ()):
            continue
        if (
            started_effect is not None
            and row["required_effect"]
            and row["required_root_id"] == publish_root_id
            and row["intent_status"] == PublicationStatus.EXECUTING
            and (row["action_id"], row["publication_id"]) == started_effect
        ):
            continue
        if (
            publish_root_id is not None
            and row["required_root_id"] == publish_root_id
            and row["required_effect"]
            and row["audit_status"] == AuditStatus.ACCEPTED
            and row["publish_status"] == PublishStatus.NOT_PUBLISHED
            and (
                row["publication_id"] is None
                or (
                    row["intent_status"] == PublicationStatus.PREPARED
                    and row["started_at"] is None
                    and row["finished_at"] is None
                )
            )
        ):
            continue
        if row["required_effect"] and row["intent_status"] not in (
            PublicationStatus.PREPARED,
            PublicationStatus.EXECUTING,
            PublicationStatus.ATTENTION,
        ):
            if row["publish_status"] == PublishStatus.SKIPPED:
                if (row["root_key"] or "").startswith(source_notice.KEY_PREFIX) or source_notice.RETIREMENT in (
                    json_object(row["metadata_json"]) or {}
                ):
                    if source_results.locally_retired_fixed_notice(
                        conn, row["root_source_id"], row["required_root_id"], row["action_id"]
                    ):
                        continue
                elif row["root_kind"] not in {"answer", "question"} and row["intent_status"] in (
                    None,
                    PublicationStatus.CONFIRMED_ABSENT,
                ):
                    continue
            if row["provider"] == "tapd":
                target = source_results.source_target(row, external_id_key="source_external_id")
                if source_results.confirmed_comment(row, target):
                    continue
            elif row["action_type"] == SourceControllerAction.GITHUB_FEEDBACK:
                if source_feedback.confirmed_receipt(conn, row["required_root_id"], row["action_id"]) is not None:
                    continue
            elif (
                row["provider"] == "github"
                and source_github_publication.confirmed_effect(conn, row["required_root_id"], row["action_id"])
                is not None
            ):
                continue
        yield RoundBlocker("publication", row["publication_id"] or row["action_id"], "publication_unresolved")


def _blockers(
    conn,
    rounds,
    limit,
    *,
    queued_task_id=None,
    source_id=None,
    candidate_action_ids=(),
    publish_root_id=None,
    started_effect=None,
):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid source blocker limit")
    result = []
    queries = (
        _worker_blockers(conn, rounds),
        _child_model_blockers(conn, rounds),
        _publication_blockers(conn, rounds, source_id, publish_root_id=publish_root_id, started_effect=started_effect),
    )
    for query in queries:
        for blocker in query:
            if queued_task_id is not None and blocker == RoundBlocker("task", queued_task_id, "task_runnable"):
                continue
            if blocker.kind == "publication" and blocker.identity in candidate_action_ids:
                continue
            result.append(blocker)
            if len(result) == limit:
                return tuple(result)
    return tuple(result)


def blockers(conn, round_id, *, limit=20):
    """Report bounded diagnostics from one round's child execution and effect facts."""
    return _blockers(conn, (round_id,), limit)


def blockers_for_source(conn, source_id, *, limit=20, queued_task_id=None):
    """Include revived execution or effects attached to any historical round."""
    round_table = tables["source_rounds"]
    rounds = select(round_table.c.round_id).where(round_table.c.source_id == source_id)
    return _blockers(conn, rounds, limit, queued_task_id=queued_task_id, source_id=source_id)


def execution_blocked_for_source(conn, source_id):
    """Check historical Worker and model ownership without ignoring a publication state."""
    table = tables["source_rounds"]
    rounds = select(table.c.round_id).where(table.c.source_id == source_id)
    return (
        next(_worker_blockers(conn, rounds), None) is not None
        or next(_child_model_blockers(conn, rounds), None) is not None
    )


def blockers_for_publication(conn, source_id, source_result_id, *, started_effect=None):
    """Exempt only accepted, unstarted required effects of this exact root."""
    table = tables["source_rounds"]
    rounds = select(table.c.round_id).where(table.c.source_id == source_id)
    return _blockers(
        conn, rounds, 1, source_id=source_id, publish_root_id=source_result_id, started_effect=started_effect
    )


def blockers_for_worker_result(conn, source_id, task_id, candidate_action_ids):
    """Check all source ownership while promoting only the exact unused candidate."""
    table = tables["source_rounds"]
    rounds = select(table.c.round_id).where(table.c.source_id == source_id)
    return _blockers(
        conn,
        rounds,
        1,
        queued_task_id=task_id,
        source_id=source_id,
        candidate_action_ids=frozenset(candidate_action_ids),
    )


def worker_attempts(conn, round_id):
    member, attempt = tables["source_round_tasks"], tables["attempts"]
    return statements.read(
        conn,
        select(attempt)
        .select_from(member.join(attempt, attempt.c.task_id == member.c.task_id))
        .where(member.c.round_id == round_id)
        .order_by(attempt.c.attempt_id),
    )


def round_launch_state(conn, round_id):
    """Distinguish a durable launch, proven no launch, and ambiguous stored evidence."""
    task, member, control = (tables[name] for name in ("tasks", "source_round_tasks", "execution_subjects"))
    task_rows = statements.read(
        conn,
        select(task.c.task_id, task.c.task_kind, task.c.lifecycle, task.c.metadata_json)
        .select_from(member.join(task, task.c.task_id == member.c.task_id))
        .where(member.c.round_id == round_id),
    )
    attempt_rows = worker_attempts(conn, round_id)
    by_task = {}
    started = False
    started_tasks = set()
    for row in attempt_rows:
        by_task.setdefault(row["task_id"], set()).add(row["attempt_id"])
        metadata = json_object(row["metadata_json"])
        status = row["status"]
        if metadata is None or status not in {value.value for value in AttemptStatus}:
            return "unknown"
        checkpoint = metadata.get("native_launch_checkpoint")
        dispatch = metadata.get("dispatch")
        termination = metadata.get("termination")
        worker_started = metadata.get("worker_started_at")
        stopped = metadata.get("process_stopped_at")
        if (
            (checkpoint is not None and utc_timestamp(checkpoint) is None)
            or (worker_started is not None and utc_timestamp(worker_started) is None)
            or (stopped is not None and utc_timestamp(stopped) is None)
            or (dispatch is not None and not isinstance(dispatch, dict))
            or (termination is not None and not isinstance(termination, dict))
        ):
            return "unknown"
        dispatch = dispatch or {}
        pid = dispatch.get("pid")
        if pid is not None and (type(pid) is not int or pid <= 1):
            return "unknown"
        if "cleanup_pending" in dispatch and type(dispatch["cleanup_pending"]) is not bool:
            return "unknown"
        if checkpoint or worker_started or pid:
            started = True
            started_tasks.add(row["task_id"])
        elif (
            status in (AttemptStatus.RUNNING, AttemptStatus.STALE, AttemptStatus.COMPLETED)
            or dispatch
            or termination
            or stopped
        ):
            return "unknown"
    for row in task_rows:
        task_meta = json_object(row["metadata_json"])
        if task_meta is None or row["task_id"] not in by_task:
            return "unknown"

        if row["task_kind"] == TaskKind.HUMAN_REQUEST:
            subject_type, subject_id = "task", row["task_id"]
        else:
            subject_type, subject_id = "remediation_episode", task_meta.get("remediation_episode_id")
        subject = records.first(
            statements.read(
                conn,
                select(control.c.metadata_json).where(
                    control.c.subject_type == subject_type, control.c.subject_id == subject_id
                ),
            )
        )
        metadata = json_object(subject[0]) if subject else None
        if metadata is None:
            return "unknown"
        inflight = metadata.get("dispatch_inflight")
        if inflight is not None:
            if not isinstance(inflight, str) or inflight not in by_task[row["task_id"]]:
                return "unknown"
            started = True
            started_tasks.add(row["task_id"])
        if row["lifecycle"] == TaskLifecycle.RUNNING and row["task_id"] not in started_tasks:
            return "unknown"
    for row in model_runs(conn, round_id):
        if row["launch_id"] is not None and row["launch_token"] is not None:
            if (
                not isinstance(row["launch_id"], str)
                or not row["launch_id"].strip()
                or type(row["launch_token"]) is not int
                or row["launch_token"] < 1
            ):
                return "unknown"
            started = True
        elif (
            row["launch_id"] is not None
            or row["launch_token"] is not None
            or row["dispatch_json"] is not None
            or row["output_json"] is not None
            or row["status"] == ModelRunStatus.RUNNING
            or source_model_blocker(row) not in (None, "model_waiting", "model_slot_reserved")
        ):
            return "unknown"
    return "started" if started else "unlaunched"


def round_has_results_or_effects(conn, round_id):
    member = tables["source_round_tasks"]
    task_ids = select(member.c.task_id).where(member.c.round_id == round_id)
    root, worker, action = (tables[name] for name in ("source_results", "worker_results", "github_actions"))
    return bool(
        statements.read(
            conn,
            select(1).where(
                or_(
                    select(root.c.source_result_id).where(root.c.round_id == round_id).exists(),
                    select(worker.c.result_id).where(worker.c.task_id.in_(task_ids)).exists(),
                    select(action.c.action_id).where(action.c.task_id.in_(task_ids)).exists(),
                )
            ),
        )
    )


def repositories_available(conn, repo_ids):
    repo = tables["repos"]
    if not repo_ids or len(set(repo_ids)) != len(repo_ids):
        return False
    enabled = statements.read(
        conn,
        select(repo.c.repo_id).where(repo.c.repo_id.in_(repo_ids), repo.c.enabled == 1),
    )
    return len(enabled) == len(repo_ids)


def workstream_adoption_blocked(conn, workstream_id):
    """Protect a pre-existing PR stream from adoption over live or unknown work."""
    task, attempt, control, action, intent, recovery = (
        tables[name]
        for name in (
            "tasks",
            "attempts",
            "execution_subjects",
            "github_actions",
            "publication_intents",
            "supervision_recovery_actions",
        )
    )
    task_ids = select(task.c.task_id).where(task.c.workstream_id == workstream_id)
    lifecycle = statements.read(conn, select(task.c.lifecycle).where(task.c.workstream_id == workstream_id))
    if any(row[0] not in TERMINAL_TASK_LIFECYCLES for row in lifecycle):
        return True
    if any(
        _attempt_blocker(row) for row in statements.read(conn, select(attempt).where(attempt.c.task_id.in_(task_ids)))
    ):
        return True
    subjects = statements.read(
        conn,
        select(control.c.metadata_json).where(control.c.subject_type == "task", control.c.subject_id.in_(task_ids)),
    )
    if any(
        (metadata := json_object(row[0])) is None or metadata.get("dispatch_inflight") is not None for row in subjects
    ):
        return True
    if statements.read(
        conn,
        select(action.c.action_id)
        .select_from(action.outerjoin(intent, intent.c.action_id == action.c.action_id))
        .where(
            action.c.task_id.in_(task_ids),
            or_(
                and_(
                    action.c.audit_status.in_((AuditStatus.PENDING.value, AuditStatus.ACCEPTED.value)),
                    action.c.publish_status == PublishStatus.NOT_PUBLISHED.value,
                ),
                intent.c.status.in_(
                    (
                        PublicationStatus.PREPARED.value,
                        PublicationStatus.EXECUTING.value,
                        PublicationStatus.ATTENTION.value,
                    )
                ),
            ),
        )
        .limit(1),
    ):
        return True
    if statements.read(
        conn,
        select(recovery.c.action_id)
        .where(
            recovery.c.scope_type == "task",
            recovery.c.scope_id.in_(task_ids),
            recovery.c.status.in_(("applying", "observing", "unknown")),
        )
        .limit(1),
    ):
        return True
    for kind in ("review", "vote"):
        session, run = (tables[name] for name in (f"{kind}_sessions", f"{kind}_runs"))
        rows = statements.read(
            conn,
            select(run)
            .select_from(session.join(run, run.c.round_id == session.c.round_id))
            .where(session.c.task_id.in_(task_ids)),
        )
        for row in rows:
            dispatch = {} if row["dispatch_json"] is None else json_object(row["dispatch_json"])
            if (
                row["slot_reserved"]
                or row["status"] in (ModelRunStatus.STARTING, ModelRunStatus.RUNNING)
                or dispatch is None
                or dispatch.get("cleanup_pending") is True
            ):
                return True
    return False
