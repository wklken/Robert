"""Exact source-owned coordination attention targets."""

from dataclasses import dataclass

from sqlalchemy import and_, case, func, or_, select

from robert_agent.foundation.contracts.delivery import SourceControllerAction
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.expressions import valid_json_or_empty
from robert_agent.persistence.queries import source_feedback, source_github_publication, source_results
from robert_agent.persistence.rounds.source_evidence import context_digest, json_object

NOTICE_TYPE = "source_coordination_attention"
QUESTION_NOTICE_TYPE = "source_ordinary_question"
PUBLICATION_NOTICE_TYPE = "source_publication_attention"
RECOVERY_NOTICE_TYPE = "source_delivery_recovery_attention"
CORRELATION = "source_attention_id"


@dataclass(frozen=True, slots=True)
class Subject:
    round_id: str
    repo_id: str | None = None
    repo_basis: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    model_run_id: str | None = None


def _source_round():
    source, session, round_ = (tables[name] for name in ("sources", "source_sessions", "source_rounds"))
    owner = source.join(session, session.c.source_id == source.c.source_id).join(
        round_, round_.c.source_id == session.c.source_id
    )
    return source, round_, owner


def _is_tapd(source):
    return and_(
        source.c.provider == "tapd",
        source.c.source_type.in_(("story", "bug")),
        source.c.host != "",
        source.c.project_key != "",
    )


def owner(conn, source_id, round_id, *, active_only=False):
    source, round_, joins = _source_round()
    return records.first(
        statements.read(
            conn,
            select(source)
            .select_from(joins)
            .where(
                source.c.source_id == source_id,
                round_.c.round_id == round_id,
                _is_tapd(source),
                round_.c.status == "active" if active_only else True,
            ),
        )
    )


def round_settled(conn, source_id, round_id):
    round_ = tables["source_rounds"]
    return bool(
        statements.read(
            conn,
            select(round_.c.round_id).where(
                round_.c.source_id == source_id,
                round_.c.round_id == round_id,
                round_.c.status != "active",
                round_.c.finished_at.is_not(None),
            ),
        )
    )


def _task_binding():
    member, task, stream, round_, source = (
        tables[name] for name in ("source_round_tasks", "tasks", "workstreams", "source_rounds", "sources")
    )
    scope = round_.alias("attention_task_round")
    origin = stream.alias("attention_task_origin")
    pr = source.alias("attention_task_pr")
    joins = (
        member.join(scope, scope.c.round_id == member.c.round_id)
        .join(task, task.c.task_id == member.c.task_id)
        .join(stream, and_(stream.c.workstream_id == task.c.workstream_id, stream.c.repo_id == member.c.repo_id))
        .outerjoin(origin, origin.c.workstream_id == stream.c.origin_workstream_id)
        .outerjoin(pr, pr.c.source_id == stream.c.primary_source_id)
        .join(
            source,
            and_(
                source.c.source_id == scope.c.source_id,
                or_(
                    stream.c.primary_source_id == scope.c.source_id,
                    and_(
                        origin.c.primary_source_id == scope.c.source_id,
                        origin.c.repo_id == member.c.repo_id,
                        pr.c.provider == "github",
                        pr.c.host == "github.com",
                        pr.c.source_type == "pull_request",
                        pr.c.repo_id == member.c.repo_id,
                    ),
                ),
            ),
        )
    )
    return member, joins


def _only_round_repo(round_, repo_id):
    repos = valid_json_or_empty(round_.c.repo_ids_json)
    return and_(func.json_array_length(repos) == 1, func.json_extract(repos, "$[0]") == repo_id)


def _repo_from_children(conn, round_id):
    member, binding = _task_binding()
    rows = statements.read(
        conn,
        select(member.c.repo_id).where(member.c.round_id == round_id).distinct().limit(2),
    )
    if not rows:
        return None, False
    if len(rows) == 1 and statements.read(
        conn,
        select(member.c.task_id)
        .select_from(binding)
        .where(member.c.round_id == round_id, member.c.repo_id == rows[0][0])
        .limit(1),
    ):
        return rows[0][0], True
    return None, True


def _selected_repo(run, repo_id):
    output = valid_json_or_empty(run.c.output_json)
    selection = func.json_each(output, "$.repositories").table_valued("value")
    return and_(
        run.c.stage == "identify",
        run.c.status == "succeeded",
        func.json_extract(output, "$.kind") == "selection",
        func.json_type(output, "$.repositories") == "array",
        select(1).select_from(selection).where(selection.c.value == repo_id).correlate(run).exists(),
    )


def _round_subject(conn, round_id, repo_id, *, model_run_id=None):
    child_repo, has_children = _repo_from_children(conn, round_id)
    if child_repo is not None and repo_id in (None, child_repo):
        return Subject(round_id, child_repo, "children", model_run_id=model_run_id)
    if model_run_id is not None or (has_children and repo_id is None):
        return None
    if repo_id is None:
        return Subject(round_id)
    round_, run = (tables[name] for name in ("source_rounds", "source_model_runs"))
    if statements.read(
        conn,
        select(round_.c.round_id).where(round_.c.round_id == round_id, _only_round_repo(round_, repo_id)),
    ):
        return Subject(round_id, repo_id, "round")
    # The coordinator supplies its actual preparation repo only after validating
    # the selection result. Bind the notice to that exact recorded model owner.
    selected = statements.read(
        conn,
        select(run.c.run_id).where(run.c.round_id == round_id, _selected_repo(run, repo_id)).limit(2),
    )
    if len(selected) == 1:
        return Subject(round_id, repo_id, "selection", model_run_id=selected[0][0])
    return None


def subject(conn, round_id, identity, *, repo_id=None):
    if identity is None:
        return _round_subject(conn, round_id, repo_id)
    member, joins = _task_binding()
    attempt, run = (tables[name] for name in ("attempts", "source_model_runs"))
    workers = statements.read(
        conn,
        select(member.c.task_id, member.c.repo_id, attempt.c.attempt_id)
        .select_from(
            joins.outerjoin(attempt, and_(attempt.c.task_id == member.c.task_id, attempt.c.attempt_id == identity))
        )
        .where(member.c.round_id == round_id, or_(member.c.task_id == identity, attempt.c.attempt_id == identity)),
    )
    models = statements.read(conn, select(run.c.stage).where(run.c.round_id == round_id, run.c.run_id == identity))
    if len(workers) == 1 and not models:
        task_id, task_repo, attempt_id = workers[0]
        if repo_id is None or repo_id == task_repo:
            return Subject(round_id, task_repo, "task", task_id, attempt_id)
    if len(models) == 1 and not workers:
        if models[0][0] == "synthesis":
            return _round_subject(conn, round_id, repo_id, model_run_id=identity)
        if models[0][0] == "identify" and repo_id is None:
            return Subject(round_id, model_run_id=identity)
    return None


def open_coordination(conn, source_id):
    attention = tables["source_attentions"]
    return records.first(
        statements.read(
            conn,
            select(attention).where(
                attention.c.source_id == source_id,
                attention.c.kind == "coordination",
                attention.c.resolved_at.is_(None),
            ),
        )
    )


def next_version(conn, source_id, *, kind="coordination"):
    attention = tables["source_attentions"]
    return statements.read(
        conn,
        select(func.coalesce(func.max(attention.c.version), 0) + 1).where(
            attention.c.source_id == source_id, attention.c.kind == kind
        ),
    )[0][0]


def _delivery_recovery():
    attention, pause, root, worker = (
        tables[name]
        for name in ("source_recovery_attentions", "source_delivery_pauses", "source_results", "worker_results")
    )
    evidence = valid_json_or_empty(root.c.evidence_json)
    saved = valid_json_or_empty(pause.c.proof_json)
    return (
        select(
            attention,
            pause.c.source_id,
            pause.c.round_id,
            worker.c.task_id,
            worker.c.attempt_id,
            func.json_extract(saved, "$.scope.repository.repo_id").label("repo_id"),
        )
        .select_from(
            attention.join(
                pause,
                and_(
                    pause.c.source_result_id == attention.c.source_result_id,
                    pause.c.generation == attention.c.generation,
                    pause.c.proof_json == attention.c.pause_proof_json,
                ),
            )
            .join(root, root.c.source_result_id == pause.c.source_result_id)
            .join(worker, worker.c.result_id == root.c.worker_result_id)
        )
        .where(
            root.c.source_id == pause.c.source_id,
            root.c.round_id == pause.c.round_id,
            func.json_extract(evidence, "$.task_id") == worker.c.task_id,
            func.json_extract(evidence, "$.attempt_id") == worker.c.attempt_id,
            func.json_extract(evidence, "$.repo_id") == func.json_extract(saved, "$.scope.repository.repo_id"),
        )
        .subquery("attention_delivery_recovery")
    )


def open_recovery(conn, source_id):
    attention, recovery = tables["source_attentions"], tables["source_recovery_attentions"]
    return records.first(
        statements.read(
            conn,
            select(attention, recovery.c.source_result_id, recovery.c.generation, recovery.c.pause_proof_json)
            .select_from(attention.join(recovery, recovery.c.notification_id == attention.c.notification_id))
            .where(
                attention.c.source_id == source_id,
                attention.c.kind == "delivery_recovery",
                attention.c.resolved_at.is_(None),
            ),
        )
    )


def _question_receipt_seal(question, root, anchor):
    """Compare the canonical snapshot made at completion with every current obligation."""
    vector = valid_json_or_empty(question.c.result_evidence_json)
    saved = func.json_each(vector, "$.receipts").table_valued("key", "value")
    link, action, intent, tapd, github = (
        tables[name].alias("question_seal_" + name)
        for name in (
            "source_required_actions",
            "github_actions",
            "publication_intents",
            "tapd_publication_preparations",
            "github_publication_preparations",
        )
    )
    valid_row = valid_json_or_empty(saved.c.value)
    fields = {
        name: action.c[name]
        for name in (
            "action_id",
            "source_result_id",
            "task_id",
            "result_id",
            "provider",
            "action_type",
            "audit_status",
            "publish_status",
            "target_json",
            "metadata_json",
            "external_id",
            "target_url",
        )
    } | {
        "position": link.c.position,
        "publication_id": intent.c.publication_id,
        "intent_status": intent.c.status,
        "intent_fingerprint": intent.c.request_fingerprint,
        "intent_started_at": intent.c.started_at,
        "intent_finished_at": intent.c.finished_at,
        "prepared_evidence_json": tapd.c.prepared_evidence_json,
        "github_prepared_evidence_json": github.c.prepared_evidence_json,
        "remote_evidence_json": intent.c.remote_evidence_json,
    }
    assert set(fields) == set(source_results.RECEIPT_FIELDS)
    joins = (
        link.join(action, action.c.action_id == link.c.action_id)
        .join(intent, intent.c.action_id == action.c.action_id)
        .outerjoin(tapd, tapd.c.publication_id == intent.c.publication_id)
        .outerjoin(github, github.c.publication_id == intent.c.publication_id)
    )
    exact = (
        select(1)
        .select_from(joins)
        .where(
            link.c.source_result_id == root.c.source_result_id,
            link.c.position == saved.c.key,
            action.c.audit_status == "accepted",
            action.c.publish_status == "published",
            intent.c.status == "published",
            *(func.json_extract(valid_row, "$." + name).is_not_distinct_from(value) for name, value in fields.items()),
        )
        .correlate(root, saved)
        .exists()
    )
    different = select(1).select_from(saved).where(~exact).correlate(question, root).exists()
    count = (
        select(func.count())
        .select_from(link)
        .where(
            link.c.source_result_id == root.c.source_result_id,
        )
        .correlate(root)
        .scalar_subquery()
    )
    anchor_member = (
        select(1)
        .select_from(saved)
        .where(
            saved.c.key == anchor.c.position,
            func.json_extract(valid_row, "$.action_id") == anchor.c.action_id,
        )
        .correlate(anchor, question)
        .exists()
    )
    return and_(
        func.json_extract(vector, "$.version") == 1,
        func.json_extract(vector, "$.root_evidence") == root.c.evidence_json,
        func.json_type(vector, "$.receipts") == "array",
        func.json_array_length(vector, "$.receipts") == count,
        count > 0,
        anchor_member,
        ~different,
    )


def _delivered_question():
    """Join the recorded projection to its exact accepted root and publication."""
    q, result, link, action, intent, prepared, worker, model = (
        tables[name]
        for name in (
            "source_question_attentions",
            "source_results",
            "source_required_actions",
            "github_actions",
            "publication_intents",
            "tapd_publication_preparations",
            "worker_results",
            "source_model_runs",
        )
    )
    other = link.alias("other_question_effect")
    evidence = valid_json_or_empty(result.c.evidence_json)
    payload = valid_json_or_empty(result.c.payload_json)
    metadata = valid_json_or_empty(action.c.metadata_json)
    output = valid_json_or_empty(model.c.output_json)
    remote = valid_json_or_empty(intent.c.remote_evidence_json)
    target = valid_json_or_empty(action.c.target_json)
    worker_question = and_(
        worker.c.result_id.is_not(None),
        worker.c.output_type == "waiting_for_user",
        func.json_extract(payload, "$.output_type") == worker.c.output_type,
        worker.c.task_id == action.c.task_id,
        worker.c.task_id == func.json_extract(evidence, "$.task_id"),
        worker.c.attempt_id == func.json_extract(evidence, "$.attempt_id"),
        action.c.result_id == worker.c.result_id,
        action.c.source_result_id.is_(None),
        func.json_extract(metadata, "$.body") == q.c.body,
    )
    model_question = and_(
        model.c.run_id.is_not(None),
        model.c.status == "succeeded",
        model.c.round_id == result.c.round_id,
        model.c.input_version == result.c.input_version,
        model.c.stage == func.json_extract(evidence, "$.stage"),
        action.c.source_result_id == result.c.source_result_id,
        action.c.task_id.is_(None),
        action.c.result_id.is_(None),
        func.json_extract(output, "$.kind") == "question",
        func.json_extract(output, "$.question") == q.c.body,
        func.json_extract(payload, "$.kind") == "question",
        func.json_extract(payload, "$.body") == q.c.body,
        func.json_extract(metadata, "$.body") == q.c.body,
    )
    return (
        select(
            q.c.notification_id,
            q.c.source_result_id,
            q.c.body,
            result.c.source_id,
            result.c.round_id,
            result.c.input_version,
            worker.c.task_id,
            worker.c.attempt_id,
            model.c.run_id,
            model.c.stage,
            func.json_extract(evidence, "$.round_input_sha256").label("input_sha256"),
            func.json_extract(evidence, "$.repo_id").label("worker_repo_id"),
            case(
                (
                    and_(model.c.stage == "synthesis", func.json_type(evidence, "$.repo_ids") == "array"),
                    func.json_extract(evidence, "$.repo_ids"),
                )
            ).label("repo_ids_json"),
            target.label("target"),
        )
        .select_from(
            q.join(result, result.c.source_result_id == q.c.source_result_id)
            .join(intent, intent.c.publication_id == q.c.publication_id)
            .join(prepared, prepared.c.publication_id == intent.c.publication_id)
            .join(action, action.c.action_id == intent.c.action_id)
            .join(
                link, and_(link.c.action_id == action.c.action_id, link.c.source_result_id == result.c.source_result_id)
            )
            .outerjoin(worker, worker.c.result_id == result.c.worker_result_id)
            .outerjoin(model, model.c.run_id == result.c.model_run_id)
        )
        .where(
            result.c.kind == "question",
            func.json_array_length(evidence, "$.handled_members") > 0,
            or_(
                and_(
                    q.c.result_evidence_json == result.c.evidence_json,
                    link.c.position == 0,
                    ~select(1)
                    .where(other.c.source_result_id == result.c.source_result_id, other.c.position != 0)
                    .exists(),
                ),
                _question_receipt_seal(q, result, link),
            ),
            action.c.provider == "tapd",
            action.c.action_type == "comment",
            action.c.audit_status == "accepted",
            action.c.publish_status == "published",
            intent.c.status == "published",
            intent.c.started_at.is_not(None),
            intent.c.finished_at.is_not(None),
            intent.c.request_fingerprint == q.c.intent_fingerprint,
            intent.c.remote_evidence_json == q.c.remote_evidence_json,
            prepared.c.prepared_evidence_json == q.c.prepared_evidence_json,
            func.json_extract(remote, "$.status") == "confirmed_effect",
            func.json_extract(remote, "$.receipt.action_id") == action.c.action_id,
            func.json_extract(remote, "$.receipt.external_id") == action.c.external_id,
            func.json_extract(remote, "$.receipt.target") == func.json(target),
            or_(worker_question, model_question),
        )
        .subquery("delivered_question")
    )


def _complete_repository_set(repo_ids, round_id, *owners):
    member, task_scope = _task_binding()
    selected = func.json_each(repo_ids).table_valued("value", "type")
    exact_repo = (
        select(1)
        .select_from(task_scope)
        .where(member.c.round_id == round_id, member.c.repo_id == selected.c.value)
        .correlate(*owners, selected)
        .exists()
    )
    missing_repo = (
        select(1).select_from(selected).where(or_(selected.c.type != "text", ~exact_repo)).correlate(*owners).exists()
    )
    selected_count = (
        select(func.count(func.distinct(selected.c.value))).select_from(selected).correlate(*owners).scalar_subquery()
    )
    child_count = (
        select(func.count(func.distinct(member.c.repo_id)))
        .where(member.c.round_id == round_id)
        .correlate(*owners)
        .scalar_subquery()
    )
    return and_(
        func.json_type(repo_ids) == "array",
        func.json_array_length(repo_ids) > 0,
        func.json_array_length(repo_ids) == selected_count,
        selected_count == child_count,
        ~missing_repo,
    )


def _question_scope(question, attention, source, round_, member, exact_task, exact_attempt):
    recovery = tables["source_delivery_recoveries"]
    finished = or_(
        and_(round_.c.status == "waiting_for_user", round_.c.finished_at == attention.c.created_at),
        and_(
            round_.c.status == "canceled",
            select(1)
            .where(
                recovery.c.source_result_id == question.c.source_result_id,
                recovery.c.source_id == attention.c.source_id,
                recovery.c.completed_at == attention.c.created_at,
                recovery.c.suspended_at.is_(None),
            )
            .correlate(attention, question)
            .exists(),
        ),
    )
    no_workers = ~select(1).where(member.c.round_id == attention.c.round_id).correlate(attention).exists()
    return and_(
        attention.c.kind == "ordinary_question",
        attention.c.cause == "source_question_delivered",
        question.c.source_id == attention.c.source_id,
        question.c.round_id == attention.c.round_id,
        question.c.input_version == round_.c.input_version,
        question.c.input_sha256 == round_.c.input_sha256,
        finished,
        func.json_extract(question.c.target, "$.provider") == "tapd",
        func.json_extract(question.c.target, "$.host") == source.c.host,
        func.json_extract(question.c.target, "$.workspace_id") == source.c.project_key,
        func.json_extract(question.c.target, "$.kind") == source.c.source_type,
        func.json_extract(question.c.target, "$.external_id") == source.c.external_id,
        or_(
            and_(
                attention.c.repo_basis == "task",
                attention.c.task_id == question.c.task_id,
                attention.c.attempt_id == question.c.attempt_id,
                attention.c.repo_id == question.c.worker_repo_id,
                exact_task,
                exact_attempt,
            ),
            and_(
                attention.c.repo_id.is_(None),
                attention.c.model_run_id == question.c.run_id,
                or_(
                    and_(question.c.stage == "identify", no_workers),
                    and_(
                        question.c.stage == "synthesis",
                        _complete_repository_set(question.c.repo_ids_json, attention.c.round_id, attention, question),
                    ),
                ),
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class _Projection:
    joins: object
    target: object
    source: object
    valid: object
    question: object
    recovery: object


def projection(notice, joins):
    """Join exact ownership once for queue selection and individual rendering."""
    attention = tables["source_attentions"]
    source = tables["sources"].alias("attention_source")
    session = tables["source_sessions"].alias("attention_session")
    round_ = tables["source_rounds"].alias("attention_round")
    member, task_scope = _task_binding()
    attempt, run = (tables[name] for name in ("attempts", "source_model_runs"))
    question = _delivered_question()
    recovery = _delivery_recovery()
    joins = (
        joins.outerjoin(attention, attention.c.notification_id == notice.c.notification_id)
        .outerjoin(session, session.c.source_id == attention.c.source_id)
        .outerjoin(round_, and_(round_.c.round_id == attention.c.round_id, round_.c.source_id == session.c.source_id))
        .outerjoin(source, source.c.source_id == round_.c.source_id)
        .outerjoin(
            question,
            and_(
                attention.c.kind == "ordinary_question",
                question.c.notification_id == attention.c.notification_id,
            ),
        )
        .outerjoin(recovery, recovery.c.notification_id == attention.c.notification_id)
    )
    exact_task = (
        select(1)
        .select_from(task_scope)
        .where(
            member.c.task_id == attention.c.task_id,
            member.c.repo_id == attention.c.repo_id,
            member.c.round_id == attention.c.round_id,
        )
        .correlate(attention)
        .exists()
    )
    child_repo = (
        select(1)
        .select_from(task_scope)
        .where(member.c.round_id == attention.c.round_id, member.c.repo_id == attention.c.repo_id)
        .correlate(attention)
        .exists()
    )
    other_repo = (
        select(1)
        .where(member.c.round_id == attention.c.round_id, member.c.repo_id != attention.c.repo_id)
        .correlate(attention)
        .exists()
    )
    exact_repo = or_(
        and_(attention.c.repo_basis.is_(None), attention.c.repo_id.is_(None), attention.c.task_id.is_(None)),
        and_(attention.c.repo_basis == "task", exact_task),
        and_(attention.c.repo_basis == "children", child_repo, ~other_repo),
        and_(attention.c.repo_basis == "round", _only_round_repo(round_, attention.c.repo_id)),
        and_(attention.c.repo_basis == "selection", attention.c.model_run_id.is_not(None)),
    )
    exact_attempt = (
        select(1)
        .where(
            attempt.c.attempt_id == attention.c.attempt_id,
            attempt.c.task_id == attention.c.task_id,
        )
        .correlate(attention)
        .exists()
    )
    exact_model = (
        select(1)
        .where(
            run.c.run_id == attention.c.model_run_id,
            run.c.round_id == attention.c.round_id,
            or_(
                and_(run.c.stage == "identify", attention.c.repo_basis.is_(None)),
                and_(run.c.stage == "synthesis", attention.c.repo_basis == "children"),
                and_(attention.c.repo_basis == "selection", _selected_repo(run, attention.c.repo_id)),
            ),
        )
        .correlate(attention)
        .exists()
    )
    metadata = valid_json_or_empty(notice.c.metadata_json)
    valid = and_(
        notice.c.task_id.is_(None),
        func.json_extract(metadata, "$." + CORRELATION) == attention.c.notification_id,
        func.json_remove(metadata, "$." + CORRELATION, "$.delivery", "$.operator_response") == "{}",
        _is_tapd(source),
        or_(
            and_(
                notice.c.notification_type == NOTICE_TYPE,
                attention.c.kind == "coordination",
                exact_repo,
                or_(attention.c.attempt_id.is_(None), exact_attempt),
                or_(attention.c.model_run_id.is_(None), exact_model),
            ),
            and_(
                notice.c.notification_type == QUESTION_NOTICE_TYPE,
                _question_scope(question, attention, source, round_, member, exact_task, exact_attempt),
            ),
            and_(
                notice.c.notification_type == RECOVERY_NOTICE_TYPE,
                attention.c.kind == "delivery_recovery",
                recovery.c.source_id == source.c.source_id,
                recovery.c.round_id == round_.c.round_id,
                round_.c.status == "canceled",
                round_.c.finished_at.is_not(None),
                attention.c.repo_basis == "task",
                attention.c.repo_id == recovery.c.repo_id,
                attention.c.task_id == recovery.c.task_id,
                attention.c.attempt_id == recovery.c.attempt_id,
                attention.c.model_run_id.is_(None),
                exact_task,
                exact_attempt,
            ),
        ),
    )
    return _Projection(joins, attention, source, valid, question, recovery)


def notice_details(conn, notification_id, *, include_closed=False):
    notice = tables["notifications"]
    scope = projection(notice, notice)
    return records.first(
        statements.read(
            conn,
            select(
                scope.target,
                scope.source.c.title,
                scope.source.c.host,
                scope.source.c.project_key,
                scope.question.c.source_result_id,
                scope.question.c.body.label("question_body"),
                scope.question.c.repo_ids_json.label("question_repos"),
                scope.recovery.c.source_result_id.label("recovery_result_id"),
                scope.recovery.c.generation.label("recovery_generation"),
            )
            .select_from(scope.joins)
            .where(
                notice.c.notification_id == notification_id,
                scope.valid,
                True if include_closed else scope.target.c.resolved_at.is_(None),
            ),
        )
    )


def _publication_subject():
    """Select real source ownership independently of mutable notification text."""
    result, link, action, intent, source, round_, worker, model, repo, attempt = (
        tables[name].alias("attention_effect_" + name)
        for name in (
            "source_results",
            "source_required_actions",
            "github_actions",
            "publication_intents",
            "sources",
            "source_rounds",
            "worker_results",
            "source_model_runs",
            "repos",
            "attempts",
        )
    )
    member, task_scope = _task_binding()
    evidence = valid_json_or_empty(result.c.evidence_json)
    target = valid_json_or_empty(action.c.target_json)
    selected = case(
        (
            and_(model.c.stage == "synthesis", func.json_type(evidence, "$.repo_ids") == "array"),
            func.json_extract(evidence, "$.repo_ids"),
        )
    )
    exact_worker = (
        select(1)
        .select_from(task_scope)
        .where(
            member.c.round_id == result.c.round_id,
            member.c.task_id == worker.c.task_id,
            member.c.repo_id == repo.c.repo_id,
        )
        .correlate(result, worker, repo)
        .exists()
    )
    round_scope = and_(
        round_.c.round_id.is_not(None),
        round_.c.source_id == result.c.source_id,
        round_.c.input_version == result.c.input_version,
        round_.c.input_sha256 == func.json_extract(evidence, "$.round_input_sha256"),
        func.json_array_length(evidence, "$.handled_members") > 0,
    )
    worker_scope = and_(
        round_scope,
        worker.c.result_id.is_not(None),
        worker.c.result_id == action.c.result_id,
        worker.c.task_id == action.c.task_id,
        worker.c.task_id == func.json_extract(evidence, "$.task_id"),
        attempt.c.attempt_id == func.json_extract(evidence, "$.attempt_id"),
        action.c.source_result_id.is_(None),
        exact_worker,
    )
    taskless_scope = and_(
        action.c.source_result_id == result.c.source_result_id,
        action.c.task_id.is_(None),
        action.c.result_id.is_(None),
        or_(
            and_(
                result.c.kind == "control_notice",
                result.c.round_id.is_(None),
                result.c.worker_result_id.is_(None),
                result.c.model_run_id.is_(None),
            ),
            and_(
                round_scope,
                model.c.round_id == result.c.round_id,
                model.c.input_version == result.c.input_version,
                model.c.stage == func.json_extract(evidence, "$.stage"),
                model.c.status == "succeeded",
                or_(
                    and_(
                        model.c.stage == "identify",
                        result.c.kind == "question",
                        ~select(1).where(member.c.round_id == result.c.round_id).correlate(result).exists(),
                    ),
                    and_(
                        model.c.stage == "synthesis",
                        _complete_repository_set(selected, result.c.round_id, result, model),
                    ),
                ),
            ),
        ),
    )
    tapd = and_(
        action.c.provider == "tapd",
        action.c.action_type == "comment",
        func.json_extract(target, "$.provider") == "tapd",
        func.json_extract(target, "$.host") == source.c.host,
        func.json_extract(target, "$.workspace_id") == source.c.project_key,
        func.json_extract(target, "$.kind") == source.c.source_type,
        func.json_extract(target, "$.external_id") == source.c.external_id,
    )
    github = and_(
        action.c.provider == "github",
        action.c.action_type.in_(("push_existing_pr", "open_pr")),
        func.json_extract(target, "$.provider") == "github",
        func.json_extract(target, "$.host") == "github.com",
        func.json_extract(target, "$.repo_id") == repo.c.repo_id,
        func.json_extract(target, "$.repository") == repo.c.full_name,
    )
    feedback_kind = action.c.action_type == SourceControllerAction.GITHUB_FEEDBACK.value
    feedback = and_(
        feedback_kind,
        action.c.provider == "github",
        round_scope,
        action.c.source_result_id == result.c.source_result_id,
        action.c.task_id.is_(None),
        action.c.result_id.is_(None),
        result.c.kind.in_(("answer", "question")),
        func.json_extract(target, "$.provider") == "github",
        func.json_extract(target, "$.host") == "github.com",
        func.json_extract(target, "$.repo_id") == repo.c.repo_id,
        func.json_extract(target, "$.repository") == repo.c.full_name,
    )
    publication_repo = case(
        (feedback_kind, func.json_extract(target, "$.repo_id")),
        else_=func.json_extract(evidence, "$.repo_id"),
    )
    return (
        select(
            result.c.source_id,
            result.c.source_result_id,
            result.c.round_id,
            action.c.action_id,
            action.c.provider,
            action.c.action_type,
            intent.c.publication_id,
            intent.c.request_fingerprint,
            intent.c.status.label("intent_status"),
            action.c.publish_status,
            source.c.title,
            source.c.host,
            source.c.project_key,
            repo.c.repo_id,
            selected.label("repo_ids_json"),
        )
        .select_from(
            link.join(result, result.c.source_result_id == link.c.source_result_id)
            .join(source, source.c.source_id == result.c.source_id)
            .join(action, action.c.action_id == link.c.action_id)
            .join(intent, intent.c.action_id == action.c.action_id)
            .outerjoin(round_, round_.c.round_id == result.c.round_id)
            .outerjoin(worker, worker.c.result_id == result.c.worker_result_id)
            .outerjoin(
                attempt, and_(attempt.c.attempt_id == worker.c.attempt_id, attempt.c.task_id == worker.c.task_id)
            )
            .outerjoin(repo, repo.c.repo_id == publication_repo)
            .outerjoin(model, model.c.run_id == result.c.model_run_id)
        )
        .where(
            _is_tapd(source),
            action.c.audit_status == "accepted",
            action.c.publish_status.in_(("not_published", "published")),
            intent.c.status.in_(("executing", "attention", "published")),
            func.julianday(intent.c.started_at).is_not(None),
            func.length(intent.c.request_fingerprint) == 64,
            intent.c.request_fingerprint.bool_op("NOT GLOB")("*[^0-9a-f]*"),
            or_(and_(worker_scope, or_(tapd, github)), and_(taskless_scope, repo.c.repo_id.is_(None), tapd), feedback),
        )
        .subquery("attention_publication_subject")
    )


def publication_subject(conn, source_id, source_result_id, action_id, provider):
    subject = _publication_subject()
    return records.first(
        statements.read(
            conn,
            select(subject).where(
                subject.c.source_id == source_id,
                subject.c.source_result_id == source_result_id,
                subject.c.action_id == action_id,
                subject.c.provider == provider,
            ),
        )
    )


def publication_confirmed(conn, subject):
    """Confirm this exact effect through the same canonical provider receipt readers."""
    root_id, action_id = subject["source_result_id"], subject["action_id"]
    if subject["action_type"] == SourceControllerAction.GITHUB_FEEDBACK:
        receipt = source_feedback.confirmed_receipt(conn, root_id, action_id)
        return receipt is not None and (receipt.publication_id, receipt.request_fingerprint) == (
            subject["publication_id"],
            subject["request_fingerprint"],
        )
    if subject["provider"] == "github":
        receipt = source_github_publication.confirmed_effect(conn, root_id, action_id)
        return receipt is not None and (receipt.publication_id, receipt.request_fingerprint) == (
            subject["publication_id"],
            subject["request_fingerprint"],
        )
    source = source_results.tapd_source(conn, subject["source_id"])
    root = source_results.for_source(conn, subject["source_id"], root_id)
    action = next(
        (row for row in source_results.required_actions(conn, root_id) if row["action_id"] == action_id), None
    )
    try:
        if (
            source is None
            or root is None
            or action is None
            or (action["publication_id"], action["intent_fingerprint"])
            != (subject["publication_id"], subject["request_fingerprint"])
        ):
            return False
        prepared = json_object(action["prepared_evidence_json"])
        if (
            prepared is None
            or context_digest(prepared) != subject["request_fingerprint"]
            or source_results.confirmed_comment(action, source_results.source_target(source)) is None
        ):
            return False
        remote = json_object(action["remote_evidence_json"])
        accepted, started, confirmed, finished = map(
            utc_timestamp,
            (
                root["accepted_at"],
                action["intent_started_at"],
                remote["receipt"]["confirmed_at"],
                action["intent_finished_at"],
            ),
        )
        return (
            all(moment is not None for moment in (accepted, started, confirmed, finished))
            and accepted <= started <= confirmed <= finished
        )
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        return False


def publication_episode(conn, publication_id):
    attention = tables["source_publication_attentions"]
    return records.first(
        statements.read(
            conn,
            select(attention)
            .where(
                attention.c.publication_id == publication_id,
            )
            .order_by(attention.c.version.desc())
            .limit(1),
        )
    )


@dataclass(frozen=True, slots=True)
class PublicationProjection:
    joins: object
    target: object
    subject: object
    valid: object


def publication_projection(notice, joins):
    target = tables["source_publication_attentions"]
    subject = _publication_subject()
    joins = joins.outerjoin(target, target.c.notification_id == notice.c.notification_id).outerjoin(
        subject,
        and_(
            subject.c.source_id == target.c.source_id,
            subject.c.source_result_id == target.c.source_result_id,
            subject.c.action_id == target.c.action_id,
            subject.c.provider == target.c.provider,
            subject.c.publication_id == target.c.publication_id,
            subject.c.request_fingerprint == target.c.request_fingerprint,
        ),
    )
    metadata = valid_json_or_empty(notice.c.metadata_json)
    valid = and_(
        notice.c.notification_type == PUBLICATION_NOTICE_TYPE,
        notice.c.task_id.is_(None),
        subject.c.publication_id.is_not(None),
        func.json_extract(metadata, "$." + CORRELATION) == target.c.notification_id,
        func.json_remove(metadata, "$." + CORRELATION, "$.delivery", "$.operator_response") == "{}",
    )
    return PublicationProjection(joins, target, subject, valid)


def publication_notice_details(conn, notification_id, *, include_closed=False):
    notice = tables["notifications"]
    scope = publication_projection(notice, notice)
    return records.first(
        statements.read(
            conn,
            select(
                scope.target,
                scope.subject.c.round_id,
                scope.subject.c.title,
                scope.subject.c.host,
                scope.subject.c.project_key,
                scope.subject.c.repo_id,
                scope.subject.c.repo_ids_json,
            )
            .select_from(scope.joins)
            .where(
                notice.c.notification_id == notification_id,
                scope.valid,
                True if include_closed else scope.target.c.resolved_at.is_(None),
            ),
        )
    )
