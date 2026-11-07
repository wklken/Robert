"""Repositories, source observations and durable work items."""

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from robert_agent.models.base import Base


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


class GitHubSource(Base):
    __tablename__ = "github_sources"

    source_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    repo_id: Mapped[str] = mapped_column(Text, ForeignKey("repos.repo_id", ondelete="CASCADE"))
    source_key: Mapped[str] = mapped_column(Text, unique=True)
    source_type: Mapped[str] = mapped_column(Text)
    number: Mapped[int] = mapped_column(Integer)
    html_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, server_default="")
    state: Mapped[str] = mapped_column(Text, server_default="open")
    author_login: Mapped[str | None] = mapped_column(Text)
    source_updated_at: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (CheckConstraint("source_type IN ('issue', 'pull_request')"),)


class GitHubEvent(Base):
    __tablename__ = "github_events"

    event_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    repo_id: Mapped[str] = mapped_column(Text, ForeignKey("repos.repo_id", ondelete="CASCADE"))
    source_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("github_sources.source_id", ondelete="CASCADE"),
    )
    event_fingerprint: Mapped[str] = mapped_column(Text, unique=True)
    event_type: Mapped[str] = mapped_column(Text)
    actor_kind: Mapped[str] = mapped_column(Text, server_default="github_user")
    actor_login: Mapped[str | None] = mapped_column(Text)
    author_association: Mapped[str | None] = mapped_column(Text)
    authorization_status: Mapped[str] = mapped_column(Text, server_default="pending")
    event_at: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, server_default="{}")


class Workstream(Base):
    __tablename__ = "workstreams"

    workstream_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    repo_id: Mapped[str] = mapped_column(Text, ForeignKey("repos.repo_id", ondelete="CASCADE"))
    primary_source_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("github_sources.source_id", ondelete="SET NULL"),
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
        ForeignKey("github_sources.source_id", ondelete="CASCADE"),
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
        ForeignKey("github_events.event_id", ondelete="CASCADE"),
        primary_key=True,
    )
    relationship: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (CheckConstraint("relationship IN ('trigger', 'pending', 'consumed', 'context')"),)


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
        ForeignKey("github_sources.source_id", ondelete="SET NULL"),
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
        CheckConstraint("origin_type IN ('web', 'github')"),
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
            sqlite_where=text("origin_source_id IS NOT NULL"),
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
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
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
        CheckConstraint("attempt_count >= 0"),
        UniqueConstraint("workstream_id", "episode_kind", "subject_key"),
        Index("idx_pr_remediation_episodes_status", "status", "updated_at"),
    )


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
