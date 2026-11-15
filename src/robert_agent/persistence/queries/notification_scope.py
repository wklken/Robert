"""Persisted target ownership for notification selection and individual access."""

from dataclasses import dataclass
import json
from typing import Literal

from sqlalchemy import and_, case, func, or_, select, tuple_

from robert_agent.foundation.contracts.delivery import NATIVE_RESULT_CONTRACT
from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.expressions import valid_json_or_empty
from robert_agent.persistence.queries import source_attention


@dataclass(frozen=True, slots=True)
class NotificationScope:
    kind: Literal["global", "repository", "tapd", "denied"]
    repo_id: str | None = None
    host: str | None = None
    workspace_id: str | None = None
    source_id: str | None = None
    attention_id: str | None = None
    required_repo_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Projection:
    joins: object
    kind: object
    repo_id: object
    host: object
    workspace_id: object
    source_id: object

    attention_id: object
    attention_open: object
    required_repo_ids_json: object

    def visible(self, repo_ids, project_repos, projects=(), *, lane_repos=None):
        selected = func.json_each(self.required_repo_ids_json).table_valued("value")
        missing_scope = (
            select(1)
            .select_from(selected)
            .where(
                ~and_(
                    tuple_(self.host, self.workspace_id, selected.c.value).in_(sorted(project_repos)),
                    True if lane_repos is None else selected.c.value.in_(sorted(lane_repos)),
                )
            )
            .correlate_except(selected)
            .exists()
        )
        return or_(
            self.kind == "global",
            and_(self.kind == "repository", True if repo_ids is None else self.repo_id.in_(sorted(repo_ids))),
            and_(
                self.kind == "tapd",
                or_(
                    and_(
                        self.required_repo_ids_json.is_(None),
                        self.repo_id.is_(None),
                        tuple_(self.host, self.workspace_id).in_(sorted(projects)),
                    ),
                    and_(
                        self.repo_id.is_not(None),
                        tuple_(self.host, self.workspace_id, self.repo_id).in_(sorted(project_repos)),
                        True if lane_repos is None else self.repo_id.in_(sorted(lane_repos)),
                    ),
                    and_(self.required_repo_ids_json.is_not(None), ~missing_scope),
                ),
            ),
        )

    def pending_attention(self):
        return or_(self.attention_id.is_(None), self.attention_open)


def _tapd(source):
    return and_(
        source.c.provider == "tapd",
        source.c.source_type.in_(("story", "bug")),
        source.c.host != "",
        source.c.project_key != "",
    )


def _private_stream(stream_id):
    stream = tables["workstreams"].alias("claimed_notification_stream")
    origin = tables["workstreams"].alias("claimed_notification_origin")
    source = tables["sources"].alias("claimed_notification_source")
    return (
        select(1)
        .select_from(
            stream.outerjoin(origin, origin.c.workstream_id == stream.c.origin_workstream_id).join(
                source, source.c.source_id.in_((stream.c.primary_source_id, origin.c.primary_source_id))
            )
        )
        .where(stream.c.workstream_id == stream_id, source.c.provider == "tapd")
        .exists()
    )


def _incident(incident_id, notice):
    incident, checkpoint = (tables[name] for name in ("supervision_incidents", "supervision_checkpoints"))
    binding = (
        select(incident.c.scope_type)
        .select_from(incident)
        .where(incident.c.incident_id == incident_id)
        .correlate(notice)
    )
    controller = binding.where(incident.c.scope_type == "controller").exists()
    repository = (
        binding.join(
            checkpoint,
            and_(
                checkpoint.c.scope_type == incident.c.scope_type,
                checkpoint.c.scope_id == incident.c.scope_id,
                checkpoint.c.rule_id == "admission",
            ),
        )
        .with_only_columns(func.json_extract(valid_json_or_empty(checkpoint.c.metadata_json), "$.repo_id"))
        .where(incident.c.scope_type.in_(("github_event", "work_item_event")))
        .scalar_subquery()
    )
    return controller, repository


def projection(notice):
    """Classify the real task/item, never a source identifier from notice metadata."""
    t = tables
    task, stream, member, round_, item, frozen, request, bridge, related, repo = (
        t[name]
        for name in (
            "tasks",
            "workstreams",
            "source_round_tasks",
            "source_rounds",
            "work_items",
            "source_round_requests",
            "ingress_requests",
            "source_github_ingress",
            "workstream_sources",
            "repos",
        )
    )
    item_stream = stream.alias("notice_item_stream")
    origin = stream.alias("notice_task_origin")
    primary, source, item_source = (
        t["sources"].alias(name)
        for name in (
            "notice_primary_source",
            "notice_round_source",
            "notice_item_source",
        )
    )
    raw = notice.c.metadata_json
    metadata = valid_json_or_empty(raw)

    def field(path):
        return func.json_extract(metadata, path)

    item_id = field("$.work_item_id")
    referenced_stream = field("$.workstream_id")
    joins = (
        notice.outerjoin(task, task.c.task_id == notice.c.task_id)
        .outerjoin(stream, stream.c.workstream_id == task.c.workstream_id)
        .outerjoin(origin, origin.c.workstream_id == stream.c.origin_workstream_id)
        .outerjoin(primary, primary.c.source_id == stream.c.primary_source_id)
        .outerjoin(member, member.c.task_id == task.c.task_id)
        .outerjoin(round_, round_.c.round_id == member.c.round_id)
        .outerjoin(source, source.c.source_id == round_.c.source_id)
        .outerjoin(frozen, and_(frozen.c.round_id == member.c.round_id, frozen.c.request_id == member.c.request_id))
        .outerjoin(request, request.c.request_id == frozen.c.request_id)
        .outerjoin(bridge, bridge.c.request_id == request.c.request_id)
        .outerjoin(
            related,
            and_(related.c.workstream_id == stream.c.workstream_id, related.c.source_id == primary.c.source_id),
        )
        .outerjoin(item, item.c.work_item_id == item_id)
        .outerjoin(item_source, item_source.c.source_id == item.c.origin_source_id)
        .outerjoin(item_stream, item_stream.c.workstream_id == item.c.workstream_id)
        .outerjoin(repo, repo.c.repo_id == func.coalesce(stream.c.repo_id, item.c.repo_id))
    )
    target_task = notice.c.task_id.is_not(None)
    target_item = item_id.is_not(None)
    item_native = and_(
        item.c.origin_type == "tapd",
        _tapd(item_source),
        item_stream.c.primary_source_id == item_source.c.source_id,
        item_stream.c.repo_id == item.c.repo_id,
        item_stream.c.origin_workstream_id.is_(None),
    )
    ordinary_item = and_(
        item.c.work_item_id.is_not(None),
        item.c.origin_type != "tapd",
        func.coalesce(item_source.c.provider, "") != "tapd",
    )
    derived = and_(
        primary.c.provider == "github",
        primary.c.host == "github.com",
        primary.c.source_type == "pull_request",
        primary.c.repo_id == stream.c.repo_id,
        origin.c.repo_id == stream.c.repo_id,
        origin.c.primary_source_id == source.c.source_id,
        origin.c.origin_workstream_id.is_(None),
        related.c.relationship.in_(("primary", "derived_pr")),
    )
    control_item = item.alias("notice_task_control_item")
    control_stream_id = case(
        (stream.c.primary_source_id == source.c.source_id, stream.c.workstream_id),
        else_=origin.c.workstream_id,
    )
    control_item_bound = (
        select(1)
        .where(
            control_item.c.origin_type == "tapd",
            control_item.c.origin_source_id == source.c.source_id,
            control_item.c.repo_id == stream.c.repo_id,
            control_item.c.workstream_id == control_stream_id,
        )
        .correlate(source, stream, origin)
        .exists()
    )
    valid_native_task = and_(
        member.c.repo_id == stream.c.repo_id,
        _tapd(source),
        control_item_bound,
        or_(request.c.repo_id.is_(None), request.c.repo_id == member.c.repo_id),
        or_(
            and_(request.c.provider == "tapd", request.c.source_id == source.c.source_id),
            and_(
                request.c.provider == "github",
                request.c.source_id == bridge.c.pr_source_id,
                bridge.c.repo_id == member.c.repo_id,
                bridge.c.tapd_source_id == source.c.source_id,
            ),
        ),
        or_(derived, stream.c.primary_source_id == source.c.source_id),
    )
    same_stream = and_(
        item.c.workstream_id == stream.c.workstream_id,
        item.c.repo_id == stream.c.repo_id,
    )
    origin_item = and_(
        item_native,
        item.c.origin_source_id == source.c.source_id,
        item.c.workstream_id == origin.c.workstream_id,
        item.c.repo_id == stream.c.repo_id,
        derived,
    )
    coherent_native_task = and_(
        valid_native_task,
        or_(~target_item, and_(same_stream, or_(item_native, ordinary_item)), origin_item),
    )
    native = and_(
        repo.c.repo_id.is_not(None),
        or_(and_(target_task, coherent_native_task), and_(~target_task, target_item, item_native)),
    )
    source_id = case((target_task, source.c.source_id), else_=item_source.c.source_id)
    host = case((target_task, source.c.host), else_=item_source.c.host)
    workspace = case((target_task, source.c.project_key), else_=item_source.c.project_key)
    claimed_source = func.coalesce(field("$.source_id"), field("$.tapd_source_id"), field("$.target.source_id"))
    claimed_native = or_(
        claimed_source.is_not(None),
        func.coalesce(field("$.source_provider"), "") == "tapd",
        func.coalesce(field("$.provider"), "") == "tapd",
        func.coalesce(field("$.target.provider"), "") == "tapd",
        func.coalesce(field("$.scope.provider"), "") == "tapd",
        field("$.workspace_id").is_not(None),
        field("$.target.workspace_id").is_not(None),
        field("$.scope.workspace_id").is_not(None),
    )
    coherent = and_(
        func.json_valid(raw) == 1,
        func.json_type(metadata) == "object",
        or_(field("$.task_id").is_(None), field("$.task_id") == notice.c.task_id),
        or_(~target_task, task.c.task_id.is_not(None)),
        or_(~target_item, item.c.work_item_id.is_not(None)),
    )

    def native_marker(column):
        metadata = valid_json_or_empty(column)
        return or_(
            func.json_extract(metadata, "$.result_contract") == NATIVE_RESULT_CONTRACT,
            func.json_extract(metadata, "$.native_result").is_not(None),
            func.json_extract(metadata, "$.native_workspace").is_not(None),
        )

    attempts = t["attempts"]
    native_attempt = (
        select(1)
        .where(
            attempts.c.task_id == task.c.task_id,
            native_marker(attempts.c.metadata_json),
        )
        .correlate(task)
        .exists()
    )
    ordinary_task = and_(
        stream.c.workstream_id.is_not(None),
        member.c.task_id.is_(None),
        ~native_attempt,
        func.coalesce(primary.c.provider, "") != "tapd",
        or_(~target_item, and_(ordinary_item, same_stream)),
    )
    ordinary = or_(and_(target_task, ordinary_task), and_(~target_task, target_item, ordinary_item))
    unbound = and_(~target_item, ~target_task, ~_private_stream(referenced_stream))
    incident_id, metadata_repo = field("$.incident_id"), field("$.repo_id")
    controller, incident_repo = _incident(incident_id, notice)
    explicit_repo = or_(metadata_repo.is_not(None), func.coalesce(field("$.repository_scoped"), 0) != 0)
    native_stream = or_(
        referenced_stream.is_(None),
        and_(target_task, referenced_stream == stream.c.workstream_id),
        and_(target_item, referenced_stream == item.c.workstream_id),
    )
    native_claims = (
        ("$.source_id", source_id),
        ("$.tapd_source_id", source_id),
        ("$.target.source_id", source_id),
        ("$.host", host),
        ("$.target.host", host),
        ("$.scope.host", host),
        ("$.workspace_id", workspace),
        ("$.target.workspace_id", workspace),
        ("$.scope.workspace_id", workspace),
        ("$.repo_id", repo.c.repo_id),
        ("$.target.repo_id", repo.c.repo_id),
    )
    attention = source_attention.projection(notice, joins)
    persisted = attention.target.c
    effect = source_attention.publication_projection(notice, attention.joins)
    effect_owner = effect.target.c
    effect_source = effect.subject.c
    attention_claimed = or_(
        persisted.notification_id.is_not(None),
        effect_owner.notification_id.is_not(None),
        notice.c.notification_type.in_(
            (
                source_attention.NOTICE_TYPE,
                source_attention.QUESTION_NOTICE_TYPE,
                source_attention.PUBLICATION_NOTICE_TYPE,
                source_attention.RECOVERY_NOTICE_TYPE,
            )
        ),
        field("$." + source_attention.CORRELATION).is_not(None),
    )
    kind = case(
        (and_(coherent, or_(attention.valid, effect.valid)), "tapd"),
        (attention_claimed, "denied"),
        (
            and_(
                coherent,
                native,
                native_stream,
                *(or_(field(path).is_(None), field(path) == expected) for path, expected in native_claims),
            ),
            "tapd",
        ),
        (and_(coherent, ~claimed_native, ordinary), "repository"),
        (and_(coherent, ~claimed_native, unbound, ~explicit_repo, or_(incident_id.is_(None), controller)), "global"),
        (
            and_(coherent, ~claimed_native, unbound, func.coalesce(metadata_repo, incident_repo).is_not(None)),
            "repository",
        ),
        else_="denied",
    )
    repo_id = case(
        (attention.valid, persisted.repo_id),
        (effect.valid, effect_source.repo_id),
        (target_task, stream.c.repo_id),
        (target_item, item.c.repo_id),
        else_=func.coalesce(metadata_repo, incident_repo),
    )
    return _Projection(
        effect.joins,
        kind,
        repo_id,
        case((attention.valid, attention.source.c.host), (effect.valid, effect_source.host), else_=host),
        case(
            (attention.valid, attention.source.c.project_key),
            (effect.valid, effect_source.project_key),
            else_=workspace,
        ),
        case((attention.valid, persisted.source_id), (effect.valid, effect_owner.source_id), else_=source_id),
        func.coalesce(persisted.notification_id, effect_owner.notification_id),
        or_(
            and_(attention.valid, persisted.resolved_at.is_(None)),
            and_(effect.valid, effect_owner.resolved_at.is_(None)),
        ),
        func.coalesce(attention.question.c.repo_ids_json, effect_source.repo_ids_json),
    )


def for_notice(conn, notification_id):
    notice = tables["notifications"]
    scope = projection(notice)
    row = records.first(
        statements.read(
            conn,
            select(
                scope.kind,
                scope.repo_id,
                scope.host,
                scope.workspace_id,
                scope.source_id,
                scope.attention_id,
                scope.required_repo_ids_json,
            )
            .select_from(scope.joins)
            .where(notice.c.notification_id == notification_id),
        )
    )
    if row is None or row[0] == "denied":
        return NotificationScope("denied")
    kind, repo_id, host, workspace, source_id, attention_id, required_repo_ids_json = row
    if kind == "tapd":
        required_repo_ids = tuple(json.loads(required_repo_ids_json)) if required_repo_ids_json is not None else ()
        return NotificationScope(kind, repo_id, host, workspace, source_id, attention_id, required_repo_ids)
    return NotificationScope(kind, repo_id)
