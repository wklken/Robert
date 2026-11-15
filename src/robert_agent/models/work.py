"""Repositories, source observations and durable work items."""

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    REAL,
    Integer,
    Text,
    text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from robert_agent.models.base import Base


class DispatchRequest(Base):
    __tablename__ = "dispatch_requests"

    request_id: Mapped[str] = mapped_column(Text, primary_key=True)
    repo_id: Mapped[str] = mapped_column(Text, ForeignKey("repos.repo_id"))
    identity_json: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="draft")
    issue_json: Mapped[str] = mapped_column(Text, server_default="{}")
    workstream_id: Mapped[str | None] = mapped_column(Text, unique=True)
    task_id: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str] = mapped_column(Text, server_default="")
    result_json: Mapped[str] = mapped_column(Text, server_default="{}")
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'accepted', 'creating', 'issue_created', 'admitted', 'delivered', 'failed', 'attention')"
        ),
        Index("idx_dispatch_requests_pending", "status", "created_at"),
    )


class Repository(Base):
    __tablename__ = "repos"

    repo_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    full_name: Mapped[str] = mapped_column(Text, unique=True)
    github_account: Mapped[str] = mapped_column(Text)
    default_base_branch: Mapped[str] = mapped_column(Text)
    repo_root: Mapped[str] = mapped_column(Text)
    worktree_root: Mapped[str] = mapped_column(Text)
    enabled: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (CheckConstraint("enabled IN (0, 1)"),)


class Actor(Base):
    __tablename__ = "actors"

    actor_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    login: Mapped[str] = mapped_column(Text, unique=True)
    actor_kind: Mapped[str] = mapped_column(Text, server_default="user")
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")


class ActorPermission(Base):
    __tablename__ = "actor_permissions"

    permission_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    repo_id: Mapped[str] = mapped_column(Text, ForeignKey("repos.repo_id", ondelete="CASCADE"))
    actor_id: Mapped[str] = mapped_column(Text, ForeignKey("actors.actor_id", ondelete="CASCADE"))
    trust_level: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    checked_at: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        CheckConstraint("trust_level IN ('trusted_trigger', 'accepted_context', 'ignored')"),
        UniqueConstraint("repo_id", "actor_id", "source"),
    )


class Source(Base):
    __tablename__ = "sources"

    source_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    repo_id: Mapped[str | None] = mapped_column(Text, ForeignKey("repos.repo_id", ondelete="CASCADE"))
    source_key: Mapped[str] = mapped_column(Text, unique=True)
    source_type: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    host: Mapped[str] = mapped_column(Text)
    project_key: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    number: Mapped[int | None] = mapped_column(Integer)
    html_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, server_default="")
    state: Mapped[str] = mapped_column(Text, server_default="open")
    author_login: Mapped[str | None] = mapped_column(Text)
    source_updated_at: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        UniqueConstraint("provider", "host", "project_key", "source_type", "external_id"),
        CheckConstraint("length(trim(host)) > 0"),
        CheckConstraint("length(trim(project_key)) > 0"),
        CheckConstraint("length(trim(external_id)) > 0"),
        CheckConstraint(
            "(provider = 'github' AND source_type IN ('issue', 'pull_request') AND repo_id IS NOT NULL"
            " AND typeof(number) = 'integer' AND number > 0 AND external_id = CAST(number AS TEXT))"
            " OR (provider = 'tapd' AND source_type IN ('story', 'bug') AND number IS NULL)"
        ),
    )


class SourceEvent(Base):
    __tablename__ = "source_events"

    event_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    repo_id: Mapped[str | None] = mapped_column(Text, ForeignKey("repos.repo_id", ondelete="CASCADE"))
    source_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("sources.source_id", ondelete="CASCADE"),
    )
    event_fingerprint: Mapped[str] = mapped_column(Text, unique=True)
    event_type: Mapped[str] = mapped_column(Text)
    actor_kind: Mapped[str] = mapped_column(Text, server_default="github_user")
    actor_login: Mapped[str | None] = mapped_column(Text)
    author_association: Mapped[str | None] = mapped_column(Text)
    authorization_status: Mapped[str] = mapped_column(Text, server_default="pending")
    event_at: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (Index("idx_source_events_recent", text("event_at DESC"), text("event_id DESC")),)


class Workstream(Base):
    __tablename__ = "workstreams"

    workstream_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    repo_id: Mapped[str] = mapped_column(Text, ForeignKey("repos.repo_id", ondelete="CASCADE"))
    primary_source_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("sources.source_id", ondelete="SET NULL"),
    )
    origin_workstream_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("workstreams.workstream_id", ondelete="SET NULL"),
    )
    lifecycle: Mapped[str] = mapped_column(Text, server_default="active")
    active_task_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        CheckConstraint("lifecycle IN ('active', 'completed', 'waiting_for_user', 'failed', 'canceled')"),
    )


class WorkstreamSource(Base):
    __tablename__ = "workstream_sources"

    workstream_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("workstreams.workstream_id", ondelete="CASCADE"),
        primary_key=True,
    )
    source_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("sources.source_id", ondelete="CASCADE"),
        primary_key=True,
    )
    relationship: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (CheckConstraint("relationship IN ('primary', 'derived_pr', 'origin_issue', 'related')"),)


class Task(Base):
    __tablename__ = "tasks"

    task_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    workstream_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("workstreams.workstream_id", ondelete="CASCADE"),
    )
    task_kind: Mapped[str] = mapped_column(Text, server_default="human_request")
    lifecycle: Mapped[str] = mapped_column(Text)
    parent_task_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("tasks.task_id", ondelete="SET NULL"),
    )
    priority: Mapped[str] = mapped_column(Text, server_default="P2")
    routing_mode: Mapped[str] = mapped_column(Text, server_default="auto")
    requested_worker: Mapped[str | None] = mapped_column(Text)
    route_id: Mapped[str | None] = mapped_column(Text)
    expected_output: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        Index("idx_tasks_recent", text("updated_at DESC"), text("task_id DESC")),
        CheckConstraint("task_kind IN ('human_request', 'ci_remediation', 'merge_conflict_remediation')"),
        CheckConstraint(
            "lifecycle IN ('detected', 'authorized', 'classified', 'queued', 'running', 'completed', "
            "'waiting_for_user', 'failed', 'canceled', 'ignored')"
        ),
        CheckConstraint("routing_mode IN ('auto', 'manual')"),
        CheckConstraint(
            "(routing_mode = 'auto' AND requested_worker IS NULL) OR (routing_mode = 'manual' AND "
            "requested_worker IS NOT NULL)"
        ),
        Index(
            "idx_workstreams_one_active_task",
            "workstream_id",
            unique=True,
            sqlite_where=text("lifecycle IN ('detected', 'authorized', 'classified', 'queued', 'running')"),
        ),
    )


class TaskEvent(Base):
    __tablename__ = "task_events"

    task_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        primary_key=True,
    )
    event_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("source_events.event_id", ondelete="CASCADE"),
        primary_key=True,
    )
    relationship: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (CheckConstraint("relationship IN ('trigger', 'context')"),)


class IngressRequest(Base):
    __tablename__ = "ingress_requests"

    request_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    source_id: Mapped[str] = mapped_column(Text, ForeignKey("sources.source_id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(Text)
    auth_type: Mapped[str] = mapped_column(Text)
    authorization_json: Mapped[str] = mapped_column(Text)
    content_snapshot_json: Mapped[str] = mapped_column(Text)
    content_version: Mapped[str] = mapped_column(Text)
    phase: Mapped[str] = mapped_column(Text, server_default="pending")
    repo_id: Mapped[str | None] = mapped_column(Text, ForeignKey("repos.repo_id", ondelete="SET NULL"))
    task_id: Mapped[str | None] = mapped_column(Text, ForeignKey("tasks.task_id", ondelete="SET NULL"))
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("provider IN ('github', 'tapd')"),
        CheckConstraint("length(trim(auth_type)) > 0"),
        CheckConstraint("length(trim(content_version)) > 0"),
        CheckConstraint("length(trim(phase)) > 0"),
    )


class SourceGitHubIngress(Base):
    __tablename__ = "source_github_ingress"

    event_id: Mapped[str] = mapped_column(Text, ForeignKey("source_events.event_id"), primary_key=True)
    request_id: Mapped[str | None] = mapped_column(Text, ForeignKey("ingress_requests.request_id"), unique=True)
    repo_id: Mapped[str] = mapped_column(Text, ForeignKey("repos.repo_id"))
    pr_source_id: Mapped[str] = mapped_column(Text, ForeignKey("sources.source_id"))
    tapd_source_id: Mapped[str | None] = mapped_column(Text, ForeignKey("sources.source_id"))
    pr_workstream_id: Mapped[str | None] = mapped_column(Text, ForeignKey("workstreams.workstream_id"))
    target_json: Mapped[str | None] = mapped_column(Text)
    first_observation_json: Mapped[str | None] = mapped_column(Text)
    latest_observation_json: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "request_id IS NULL OR (tapd_source_id IS NOT NULL AND pr_workstream_id IS NOT NULL "
            "AND target_json IS NOT NULL)"
        ),
        Index("idx_source_github_ingress_source", "tapd_source_id", "repo_id", "created_at", "event_id"),
    )


class SourceGitHubObservation(Base):
    __tablename__ = "source_github_observations"

    request_id: Mapped[str] = mapped_column(Text, ForeignKey("source_github_ingress.request_id"), primary_key=True)
    operation_id: Mapped[str] = mapped_column(Text)
    scope_digest: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[str] = mapped_column(Text)
    proof_digest: Mapped[str] = mapped_column(Text)

    __table_args__ = (CheckConstraint("status IN ('present', 'absent', 'unavailable')"),)


class RequestEvent(Base):
    __tablename__ = "request_events"

    row_id: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=True)
    task_id: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text, ForeignKey("ingress_requests.request_id", ondelete="CASCADE"))
    event_id: Mapped[str] = mapped_column(Text, ForeignKey("source_events.event_id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    consumed_at: Mapped[str | None] = mapped_column(Text)
    result_id: Mapped[str | None] = mapped_column(Text, ForeignKey("worker_results.result_id", ondelete="SET NULL"))
    source_result_id: Mapped[str | None] = mapped_column(Text, ForeignKey("source_results.source_result_id"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "event_id"], ["task_events.task_id", "task_events.event_id"], ondelete="CASCADE"
        ),
        CheckConstraint("state IN ('pending', 'consumed', 'revoked')"),
        CheckConstraint("(task_id IS NOT NULL) != (request_id IS NOT NULL)"),
        CheckConstraint("state = 'consumed' OR (consumed_at IS NULL AND result_id IS NULL)"),
        CheckConstraint(
            "source_result_id IS NULL OR (request_id IS NOT NULL AND state = 'consumed' "
            "AND consumed_at IS NOT NULL AND result_id IS NULL)"
        ),
        Index("idx_request_events_task", "task_id", "event_id", unique=True, sqlite_where=text("task_id IS NOT NULL")),
        Index(
            "idx_request_events_request",
            "request_id",
            "event_id",
            unique=True,
            sqlite_where=text("request_id IS NOT NULL"),
        ),
    )


class RouteDecision(Base):
    __tablename__ = "route_decisions"

    route_decision_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    task_id: Mapped[str] = mapped_column(Text, ForeignKey("tasks.task_id", ondelete="CASCADE"))
    route_id: Mapped[str] = mapped_column(Text)
    expected_output: Mapped[str] = mapped_column(Text)
    allowed_github_actions_json: Mapped[str] = mapped_column(Text, server_default="[]")
    required_skills_json: Mapped[str] = mapped_column(Text, server_default="[]")
    recommended_skills_json: Mapped[str] = mapped_column(Text, server_default="[]")
    confidence: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (CheckConstraint("confidence IN ('high', 'medium', 'low')"),)


class WorkItem(Base):
    __tablename__ = "work_items"

    work_item_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    repo_id: Mapped[str] = mapped_column(Text, ForeignKey("repos.repo_id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, server_default="")
    priority: Mapped[str] = mapped_column(Text, server_default="P2")
    origin_type: Mapped[str] = mapped_column(Text)
    origin_source_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("sources.source_id", ondelete="SET NULL"),
    )
    routing_mode: Mapped[str] = mapped_column(Text, server_default="auto")
    requested_worker: Mapped[str | None] = mapped_column(Text)
    workstream_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("workstreams.workstream_id", ondelete="SET NULL"),
    )
    creation_idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    created_by: Mapped[str] = mapped_column(Text)
    activated_at: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[str | None] = mapped_column(Text)
    canceled_at: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        CheckConstraint("priority IN ('P0', 'P1', 'P2', 'P3')"),
        CheckConstraint("origin_type IN ('web', 'github', 'tapd')"),
        CheckConstraint("routing_mode IN ('auto', 'manual')"),
        CheckConstraint("version >= 1"),
        CheckConstraint(
            "(routing_mode = 'auto' AND requested_worker IS NULL) OR (routing_mode = 'manual' AND "
            "requested_worker IS NOT NULL)"
        ),
        Index(
            "idx_work_items_board_order",
            text("updated_at DESC"),
            text("work_item_id DESC"),
        ),
        Index(
            "idx_work_items_origin_source",
            "origin_source_id",
            unique=True,
            sqlite_where=text("origin_source_id IS NOT NULL AND origin_type != 'tapd'"),
        ),
        Index(
            "idx_work_items_tapd_origin_repo",
            "origin_source_id",
            "repo_id",
            unique=True,
            sqlite_where=text("origin_source_id IS NOT NULL AND origin_type = 'tapd'"),
        ),
        Index(
            "idx_work_items_repo_updated",
            "repo_id",
            text("updated_at DESC"),
            text("work_item_id DESC"),
        ),
        Index(
            "idx_work_items_workstream",
            "workstream_id",
            unique=True,
            sqlite_where=text("workstream_id IS NOT NULL"),
        ),
    )


class WorkItemEvent(Base):
    __tablename__ = "work_item_events"

    event_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    work_item_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("work_items.work_item_id", ondelete="CASCADE"),
    )
    event_type: Mapped[str] = mapped_column(Text)
    actor_kind: Mapped[str] = mapped_column(Text)
    actor_identity: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, server_default="")
    resolves_event_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("work_item_events.event_id", ondelete="SET NULL"),
    )
    idempotency_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        Index(
            "idx_work_item_events_idempotency",
            "work_item_id",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "idx_work_item_events_resolves",
            "resolves_event_id",
            sqlite_where=text("resolves_event_id IS NOT NULL"),
        ),
        Index(
            "idx_work_item_events_timeline",
            "work_item_id",
            text("created_at DESC"),
            text("event_id DESC"),
        ),
    )


class PrRemediationEpisode(Base):
    __tablename__ = "pr_remediation_episodes"

    episode_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    workstream_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("workstreams.workstream_id", ondelete="CASCADE"),
    )
    episode_kind: Mapped[str] = mapped_column(Text)
    subject_key: Mapped[str] = mapped_column(Text)
    observed_head_sha: Mapped[str] = mapped_column(Text)
    observed_base_sha: Mapped[str] = mapped_column(Text)
    result_head_sha: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    failure_signature: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
    terminal_at: Mapped[str | None] = mapped_column(Text)
    last_task_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("tasks.task_id", ondelete="SET NULL"),
    )
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        CheckConstraint("episode_kind IN ('ci', 'merge_conflict')"),
        CheckConstraint(
            "status IN ('open', 'queued', 'remediating', 'waiting_for_signal', 'resolved', "
            "'exhausted', 'superseded', 'canceled')"
        ),
        UniqueConstraint("workstream_id", "episode_kind", "subject_key"),
        Index("idx_pr_remediation_episodes_status", "status", "updated_at"),
    )


class RepairCycle(Base):
    __tablename__ = "repair_cycles"

    cycle_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    scope: Mapped[str] = mapped_column(Text)
    episode_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("pr_remediation_episodes.episode_id", ondelete="CASCADE"), unique=True
    )
    source_id: Mapped[str | None] = mapped_column(Text, ForeignKey("sources.source_id"))
    pr_source_id: Mapped[str | None] = mapped_column(Text, ForeignKey("sources.source_id"))
    policy_version: Mapped[int | None] = mapped_column(Integer)
    policy_json: Mapped[str | None] = mapped_column(Text)
    policy_cap_json: Mapped[str | None] = mapped_column(Text)
    authorized_base_ref: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    close_reason: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[str | None] = mapped_column(Text)
    observation_digest: Mapped[str | None] = mapped_column(Text)
    legacy_attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("scope IN ('github','tapd')"),
        CheckConstraint("status IN ('active','paused','closed')"),
        CheckConstraint("legacy_attempt_count >= 0"),
        CheckConstraint(
            "(scope = 'github' AND episode_id IS NOT NULL AND source_id IS NULL AND pr_source_id IS NULL)"
            " OR (scope = 'tapd' AND episode_id IS NULL AND source_id IS NOT NULL AND pr_source_id IS NOT NULL"
            " AND policy_version IS NOT NULL AND policy_json IS NOT NULL AND authorized_base_ref IS NOT NULL"
            " AND legacy_attempt_count = 0)"
        ),
        Index(
            "idx_repair_cycles_native_open",
            "source_id",
            "pr_source_id",
            unique=True,
            sqlite_where=text("scope='tapd' AND status IN ('active','paused')"),
        ),
    )


class RepairCycleClaim(Base):
    __tablename__ = "repair_cycle_claims"

    cycle_id: Mapped[str] = mapped_column(
        Text, ForeignKey("repair_cycles.cycle_id", ondelete="CASCADE"), primary_key=True
    )
    request_key: Mapped[str] = mapped_column(Text, primary_key=True)
    episode_id: Mapped[str] = mapped_column(Text, ForeignKey("pr_remediation_episodes.episode_id", ondelete="CASCADE"))
    task_id: Mapped[str | None] = mapped_column(Text, ForeignKey("tasks.task_id", ondelete="SET NULL"))
    round_id: Mapped[str | None] = mapped_column(Text, ForeignKey("source_rounds.round_id"))
    created_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        Index(
            "idx_repair_cycle_claims_native_task",
            "task_id",
            unique=True,
            sqlite_where=text("round_id IS NOT NULL"),
        ),
        Index(
            "idx_repair_cycle_claims_native_request",
            "request_key",
            unique=True,
            sqlite_where=text("round_id IS NOT NULL"),
        ),
    )


class RepairCycleUsage(Base):
    __tablename__ = "repair_cycle_usage"

    attempt_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("attempts.attempt_id", ondelete="CASCADE"), primary_key=True, nullable=True
    )
    cycle_id: Mapped[str] = mapped_column(Text, ForeignKey("repair_cycles.cycle_id", ondelete="CASCADE"))
    tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(REAL)
    observed_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (CheckConstraint("tokens >= 0"), CheckConstraint("cost_usd >= 0"))


class PrCiObservation(Base):
    __tablename__ = "pr_ci_observations"

    observation_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    episode_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("pr_remediation_episodes.episode_id", ondelete="CASCADE"),
    )
    source_kind: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    attempt_no: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    check_name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    conclusion: Mapped[str | None] = mapped_column(Text)
    details_url: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[str | None] = mapped_column(Text)
    failure_signature: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        CheckConstraint("source_kind IN ('workflow_run', 'check_run')"),
        CheckConstraint("attempt_no >= 1"),
        UniqueConstraint("source_kind", "external_id", "attempt_no"),
        Index("idx_pr_ci_observations_episode", "episode_id", "completed_at"),
    )


class SourceSession(Base):
    __tablename__ = "source_sessions"

    source_id: Mapped[str] = mapped_column(Text, ForeignKey("sources.source_id"), primary_key=True)
    owner: Mapped[str] = mapped_column(Text)
    token: Mapped[int] = mapped_column(Integer)
    lease_until: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (CheckConstraint("token > 0"),)


class SourceRound(Base):
    __tablename__ = "source_rounds"

    round_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, ForeignKey("source_sessions.source_id"))
    status: Mapped[str] = mapped_column(Text)
    capability: Mapped[str] = mapped_column(Text)
    repo_ids_json: Mapped[str] = mapped_column(Text)
    publication_scope_json: Mapped[str] = mapped_column(Text)
    input_version: Mapped[str] = mapped_column(Text)
    input_snapshot_json: Mapped[str] = mapped_column(Text)
    input_sha256: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("status IN ('active','execution_finished','waiting_for_user','failed','canceled')"),
        CheckConstraint("capability IN ('readonly','development')"),
        CheckConstraint("(status = 'active') = (finished_at IS NULL)"),
        Index("idx_source_rounds_source", "source_id"),
        Index("idx_source_rounds_active", "source_id", unique=True, sqlite_where=text("status='active'")),
    )


class SourcePRSelection(Base):
    __tablename__ = "source_pr_selections"

    round_id: Mapped[str] = mapped_column(Text, ForeignKey("source_rounds.round_id"), primary_key=True)
    repo_id: Mapped[str] = mapped_column(Text, ForeignKey("repos.repo_id"), primary_key=True)
    observed_at: Mapped[str] = mapped_column(Text)
    decision_json: Mapped[str] = mapped_column(Text)


class SourceRoundRequest(Base):
    __tablename__ = "source_round_requests"

    round_id: Mapped[str] = mapped_column(Text, ForeignKey("source_rounds.round_id"), primary_key=True)
    request_id: Mapped[str] = mapped_column(Text, ForeignKey("ingress_requests.request_id"), primary_key=True)
    content_version: Mapped[str] = mapped_column(Text)
    content_snapshot_json: Mapped[str] = mapped_column(Text)

    __table_args__ = (Index("idx_source_round_requests_request", "request_id"),)


class SourceRoundTask(Base):
    __tablename__ = "source_round_tasks"

    task_id: Mapped[str] = mapped_column(Text, ForeignKey("tasks.task_id"), primary_key=True)
    round_id: Mapped[str] = mapped_column(Text)
    request_id: Mapped[str] = mapped_column(Text)
    repo_id: Mapped[str] = mapped_column(Text, ForeignKey("repos.repo_id"))
    capability: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        ForeignKeyConstraint(
            ["round_id", "request_id"], ["source_round_requests.round_id", "source_round_requests.request_id"]
        ),
        CheckConstraint("capability IN ('readonly','development')"),
        Index("idx_source_round_tasks_round", "round_id", "created_at", "task_id"),
    )


class TapdProjectIntake(Base):
    __tablename__ = "tapd_project_intake"

    host: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, primary_key=True)
    activated_after: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)


class SourceRoundCancellation(Base):
    __tablename__ = "source_round_cancellations"

    round_id: Mapped[str] = mapped_column(Text, ForeignKey("source_rounds.round_id"), primary_key=True)
    reason: Mapped[str] = mapped_column(Text)
    requested_at: Mapped[str] = mapped_column(Text)
