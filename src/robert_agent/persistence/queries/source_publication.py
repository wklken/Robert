"""Local, bounded selection of due native source effects."""

from sqlalchemy import and_, case, func, or_, select, tuple_

from robert_agent.foundation.contracts.delivery import (
    AuditStatus,
    GitHubActionType,
    PublicationStatus,
    PublishStatus,
    SourceControllerAction,
    SourceResultKind,
)
from robert_agent.foundation.contracts.work import ACTIVE_TASK_LIFECYCLES, SourceRoundStatus, TaskLifecycle
from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.expressions import valid_json_or_empty

STARTED = (PublicationStatus.EXECUTING.value, PublicationStatus.ATTENTION.value)


def available_lease(session, owner, now):
    return or_(
        session.c.source_id.is_(None),
        session.c.owner == owner,
        func.julianday(session.c.lease_until) <= func.julianday(now),
    )


def due_action(action, now):
    metadata = valid_json_or_empty(action.c.metadata_json)
    retryable = func.json_extract(metadata, "$.publish.retryable")
    next_due = func.json_extract(metadata, "$.publish.next_retry_at")
    return and_(
        or_(retryable.is_(None), retryable == 1),
        or_(next_due.is_(None), func.julianday(next_due) <= func.julianday(now)),
    )


def configured_repository(source, action, repo, repositories):
    """Direct source-owned actions have project scope and no repository owner."""
    return or_(
        action.c.task_id.is_(None),
        and_(repo.c.enabled == 1, tuple_(source.c.project_key, repo.c.full_name).in_(repositories)),
    )


def action_task_scope(action, stream, *, repo_id, task_ids, action_ids):
    return (
        True if repo_id is None else stream.c.repo_id == repo_id,
        True if task_ids is None else action.c.task_id.in_(task_ids),
        True if action_ids is None else action.c.action_id.in_(action_ids),
    )


def ready_actions(
    conn,
    *,
    scopes,
    repositories,
    owner,
    now,
    may_write_tapd,
    read_tapd,
    may_write_github,
    read_github,
    github_repositories,
    limit,
    repo_id=None,
    task_ids=None,
    action_ids=None,
):
    """Select due work and the owning source result before applying the limit."""
    link, action, root, source, session, round_, cancel, intent, task, stream, repo, member, result, preparation = (
        tables[name]
        for name in (
            "source_required_actions",
            "github_actions",
            "source_results",
            "sources",
            "source_sessions",
            "source_rounds",
            "source_round_cancellations",
            "publication_intents",
            "tasks",
            "workstreams",
            "repos",
            "source_round_tasks",
            "worker_results",
            "github_publication_preparations",
        )
    )
    earlier = link.alias("earlier_required_native_action")
    prior_action = action.alias("prior_native_action")
    prior_intent = intent.alias("prior_native_intent")
    prior_preparation = tables["github_publication_preparations"]
    prior_tapd = tables["tapd_publication_preparations"]
    feedback_kind = action.c.action_type == SourceControllerAction.GITHUB_FEEDBACK.value
    prior_feedback = prior_action.c.action_type == SourceControllerAction.GITHUB_FEEDBACK.value
    prior_code_receipt = and_(
        prior_action.c.provider == "github",
        prior_preparation.c.publication_id.is_not(None),
        func.json_extract(valid_json_or_empty(prior_intent.c.remote_evidence_json), "$.status")
        == case((and_(feedback_kind, prior_feedback), "confirmed_feedback"), else_="confirmed_effect"),
    )
    prior_tapd_receipt = and_(
        feedback_kind,
        prior_action.c.provider == "tapd",
        prior_action.c.action_type == "comment",
        prior_tapd.c.publication_id.is_not(None),
        func.json_type(valid_json_or_empty(prior_intent.c.remote_evidence_json), "$.receipt") == "object",
    )
    # Page only structurally settled prefixes. The writer validates full
    # cryptographic and remote receipt proof and paces malformed candidates.
    unresolved_predecessor = (
        select(earlier.c.action_id)
        .select_from(
            earlier.join(prior_action, prior_action.c.action_id == earlier.c.action_id)
            .outerjoin(prior_intent, prior_intent.c.action_id == earlier.c.action_id)
            .outerjoin(prior_preparation, prior_preparation.c.publication_id == prior_intent.c.publication_id)
            .outerjoin(prior_tapd, prior_tapd.c.publication_id == prior_intent.c.publication_id)
        )
        .where(
            earlier.c.source_result_id == link.c.source_result_id,
            earlier.c.position < link.c.position,
            or_(
                prior_action.c.publish_status != PublishStatus.PUBLISHED.value,
                prior_intent.c.publication_id.is_(None),
                prior_intent.c.status != PublicationStatus.PUBLISHED.value,
                ~func.coalesce(or_(prior_code_receipt, prior_tapd_receipt), False),
            ),
        )
        .correlate(link, action)
        .exists()
    )
    other_round = round_.alias("publication_source_round")
    other_task = task.alias("publication_source_task")
    executing_worker = (
        select(other_task.c.task_id)
        .select_from(
            other_round.join(member, member.c.round_id == other_round.c.round_id).join(
                other_task, other_task.c.task_id == member.c.task_id
            )
        )
        .where(other_round.c.source_id == source.c.source_id, other_task.c.lifecycle.in_(ACTIVE_TASK_LIFECYCLES))
        .correlate(source)
        .exists()
    )
    model_repo = repo.alias("publication_model_repository")
    round_repo = tuple_(source.c.project_key, model_repo.c.full_name).in_(repositories)
    model_repo_ids = func.json_each(valid_json_or_empty(root.c.evidence_json), "$.repo_ids").table_valued(
        "key", "value"
    )
    unmapped_model_repo = (
        select(model_repo_ids.c.value)
        .select_from(model_repo_ids.outerjoin(model_repo, model_repo.c.repo_id == model_repo_ids.c.value))
        .where(or_(model_repo.c.repo_id.is_(None), model_repo.c.enabled != 1, ~round_repo))
        .correlate(root, source)
        .exists()
    )
    started = and_(intent.c.status.in_(STARTED), intent.c.started_at.is_not(None))
    recovery = tables["source_delivery_recoveries"]
    exact_recovery = (
        select(recovery.c.source_result_id)
        .where(
            recovery.c.source_result_id == root.c.source_result_id,
            recovery.c.source_id == source.c.source_id,
            recovery.c.completed_at.is_(None),
            recovery.c.suspended_at.is_(None),
        )
        .correlate(root, source)
        .exists()
    )
    unstarted = and_(
        or_(
            intent.c.publication_id.is_(None),
            and_(
                intent.c.status == PublicationStatus.PREPARED.value,
                intent.c.started_at.is_(None),
                intent.c.finished_at.is_(None),
            ),
        ),
        ~unresolved_predecessor,
        ~executing_worker,
        ~unmapped_model_repo,
        or_(
            and_(
                cancel.c.round_id.is_(None),
                or_(
                    and_(root.c.round_id.is_(None), root.c.kind == SourceResultKind.CONTROL_NOTICE.value),
                    round_.c.status.in_(
                        (
                            SourceRoundStatus.ACTIVE.value,
                            SourceRoundStatus.EXECUTION_FINISHED.value,
                            SourceRoundStatus.WAITING_FOR_USER.value,
                        ),
                    ),
                ),
            ),
            and_(
                action.c.provider.in_(("tapd", "github")),
                cancel.c.round_id.is_not(None),
                round_.c.status == SourceRoundStatus.CANCELED.value,
                exact_recovery,
            ),
        ),
        or_(
            action.c.task_id.is_(None),
            and_(
                task.c.lifecycle == TaskLifecycle.COMPLETED.value,
                configured_repository(source, action, repo, repositories),
            ),
        ),
    )
    target = valid_json_or_empty(action.c.target_json)
    evidence = valid_json_or_empty(root.c.evidence_json)
    worker_action = and_(
        action.c.source_result_id.is_(None),
        action.c.result_id == root.c.worker_result_id,
        action.c.task_id == func.json_extract(evidence, "$.task_id"),
    )
    tapd = and_(
        read_tapd,
        action.c.provider == "tapd",
        action.c.action_type == "comment",
        or_(
            and_(
                root.c.worker_result_id.is_(None),
                action.c.source_result_id == root.c.source_result_id,
                action.c.task_id.is_(None),
                action.c.result_id.is_(None),
            ),
            worker_action,
        ),
        func.json_type(target) == "object",
        (
            select(func.count())
            .select_from(func.json_each(target).table_valued("key", "value"))
            .correlate(action)
            .scalar_subquery()
        )
        == 5,
        func.json_extract(target, "$.provider") == "tapd",
        func.json_extract(target, "$.host") == source.c.host,
        func.json_extract(target, "$.workspace_id") == source.c.project_key,
        func.json_extract(target, "$.kind") == source.c.source_type,
        func.json_extract(target, "$.external_id") == source.c.external_id,
        or_(started, unstarted) if may_write_tapd else started,
    )
    github = and_(
        read_github,
        action.c.provider == "github",
        action.c.action_type.in_((GitHubActionType.PUSH_EXISTING_PR.value, GitHubActionType.OPEN_PR.value)),
        worker_action,
        result.c.task_id == action.c.task_id,
        member.c.repo_id == stream.c.repo_id,
        func.json_extract(evidence, "$.attempt_id") == result.c.attempt_id,
        func.json_extract(evidence, "$.repo_id") == repo.c.repo_id,
        repo.c.full_name.in_(github_repositories),
        func.json_extract(target, "$.provider") == "github",
        func.json_extract(target, "$.host") == "github.com",
        func.json_extract(target, "$.repo_id") == repo.c.repo_id,
        func.json_extract(target, "$.repository") == repo.c.full_name,
        or_(
            func.json_extract(target, "$.kind") == "repository",
            and_(
                action.c.action_type == GitHubActionType.PUSH_EXISTING_PR.value,
                func.json_extract(target, "$.kind") == "pull_request",
            ),
        ),
        or_(
            and_(started, preparation.c.publication_id.is_not(None)),
            and_(may_write_github, unstarted),
        ),
    )
    feedback_repo = repo.alias("feedback_target_repository")
    feedback = and_(
        read_github,
        action.c.provider == "github",
        feedback_kind,
        action.c.source_result_id == root.c.source_result_id,
        action.c.task_id.is_(None),
        action.c.result_id.is_(None),
        root.c.kind.in_((SourceResultKind.ANSWER.value, SourceResultKind.QUESTION.value)),
        round_.c.source_id == source.c.source_id,
        feedback_repo.c.full_name.in_(github_repositories),
        feedback_repo.c.enabled == 1,
        feedback_repo.c.github_account.is_not(None),
        feedback_repo.c.github_account != "",
        tuple_(source.c.project_key, feedback_repo.c.full_name).in_(repositories),
        func.json_extract(target, "$.provider") == "github",
        func.json_extract(target, "$.host") == "github.com",
        func.json_extract(target, "$.repository") == feedback_repo.c.full_name,
        or_(
            and_(started, preparation.c.publication_id.is_not(None)),
            and_(
                may_write_github,
                unstarted,
                cancel.c.round_id.is_(None),
                round_.c.status == SourceRoundStatus.ACTIVE.value,
            ),
        ),
    )
    scoped_repo = case((feedback_kind, feedback_repo.c.repo_id), else_=stream.c.repo_id)
    query = (
        select(
            action.c.action_id,
            action.c.created_at,
            action.c.provider,
            action.c.action_type,
            case((feedback_kind, feedback_repo.c.full_name), else_=repo.c.full_name).label("native_repository"),
            link.c.source_result_id.label("native_source_result_id"),
        )
        .select_from(
            link.join(action, action.c.action_id == link.c.action_id)
            .join(root, root.c.source_result_id == link.c.source_result_id)
            .join(source, source.c.source_id == root.c.source_id)
            .outerjoin(session, session.c.source_id == source.c.source_id)
            .outerjoin(round_, round_.c.round_id == root.c.round_id)
            .outerjoin(cancel, cancel.c.round_id == root.c.round_id)
            .outerjoin(intent, intent.c.action_id == action.c.action_id)
            .outerjoin(task, task.c.task_id == action.c.task_id)
            .outerjoin(stream, stream.c.workstream_id == task.c.workstream_id)
            .outerjoin(repo, repo.c.repo_id == stream.c.repo_id)
            .outerjoin(feedback_repo, feedback_repo.c.repo_id == func.json_extract(target, "$.repo_id"))
            .outerjoin(result, result.c.result_id == root.c.worker_result_id)
            .outerjoin(member, and_(member.c.round_id == root.c.round_id, member.c.task_id == action.c.task_id))
            .outerjoin(preparation, preparation.c.publication_id == intent.c.publication_id)
        )
        .where(
            source.c.provider == "tapd",
            tuple_(source.c.host, source.c.project_key).in_(scopes),
            action.c.audit_status == AuditStatus.ACCEPTED.value,
            action.c.publish_status == PublishStatus.NOT_PUBLISHED.value,
            available_lease(session, owner, now),
            due_action(action, now),
            or_(tapd, github, feedback),
            True if repo_id is None else scoped_repo == repo_id,
            *action_task_scope(action, stream, repo_id=None, task_ids=task_ids, action_ids=action_ids),
        )
        .order_by(action.c.created_at, action.c.action_id)
        .limit(limit)
    )
    return tuple(statements.read(conn, query))


def action_is_due(conn, action_id, now):
    action = tables["github_actions"]
    return bool(
        statements.read(
            conn, select(action.c.action_id).where(action.c.action_id == action_id, due_action(action, now))
        )
    )
