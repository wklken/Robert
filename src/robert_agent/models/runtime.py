"""Daemon, run, step and audit records."""

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


class AgentRun(Base):
    __tablename__ = "agent_runs"

    run_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    status: Mapped[str] = mapped_column(Text)
    started_at: Mapped[str] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)
    config_path: Mapped[str | None] = mapped_column(Text)
    dry_run: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    summary_json: Mapped[str] = mapped_column(Text, server_default="{}")
    error_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("status IN ('running', 'completed', 'failed', 'skipped')"),
        CheckConstraint("dry_run IN (0, 1)"),
        Index("idx_agent_runs_retention", "status", text("julianday(finished_at)")),
        Index("idx_agent_runs_started", text("started_at DESC"), text("run_id DESC")),
        Index(
            "idx_agent_runs_detail_compaction",
            "status",
            text(
                "json_extract(CASE WHEN json_valid(summary_json) THEN summary_json ELSE '{}' END, '$.diagnostics.detail_status')"
            ),
            text("julianday(finished_at)"),
        ),
    )


class RunStep(Base):
    __tablename__ = "run_steps"

    step_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    run_id: Mapped[str] = mapped_column(Text, ForeignKey("agent_runs.run_id", ondelete="CASCADE"))
    step_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)
    output_json: Mapped[str | None] = mapped_column(Text)
    error_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("status IN ('pending', 'running', 'succeeded', 'failed', 'skipped')"),
        UniqueConstraint("run_id", "step_key"),
    )


class RunRepoStep(Base):
    __tablename__ = "run_repo_steps"

    step_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    run_id: Mapped[str] = mapped_column(Text, ForeignKey("agent_runs.run_id", ondelete="CASCADE"))
    repo_id: Mapped[str] = mapped_column(Text, ForeignKey("repos.repo_id", ondelete="CASCADE"))
    step_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)
    output_json: Mapped[str | None] = mapped_column(Text)
    error_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("status IN ('pending', 'running', 'succeeded', 'failed', 'skipped')"),
        UniqueConstraint("run_id", "repo_id", "step_key"),
        Index("idx_run_repo_steps_consumer_completed", "repo_id", "step_key", "status", "finished_at"),
    )


class DaemonRun(Base):
    __tablename__ = "daemon_runs"

    daemon_run_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    status: Mapped[str] = mapped_column(Text)
    owner_id: Mapped[str] = mapped_column(Text)
    config_path: Mapped[str] = mapped_column(Text)
    started_at: Mapped[str] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)
    summary_json: Mapped[str] = mapped_column(Text, server_default="{}")
    error_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (CheckConstraint("status IN ('running', 'stopped', 'failed', 'replaced')"),)


class DaemonEvent(Base):
    __tablename__ = "daemon_events"

    daemon_event_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    daemon_run_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("daemon_runs.daemon_run_id", ondelete="CASCADE"),
    )
    event_type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        Index("idx_daemon_events_retention", text("julianday(created_at)")),
        Index("idx_daemon_events_run_created", "daemon_run_id", "created_at"),
        Index("idx_daemon_events_type_created", "event_type", "created_at"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    audit_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    repo_id: Mapped[str | None] = mapped_column(Text, ForeignKey("repos.repo_id", ondelete="SET NULL"))
    workstream_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("workstreams.workstream_id", ondelete="SET NULL"),
    )
    task_id: Mapped[str | None] = mapped_column(Text, ForeignKey("tasks.task_id", ondelete="SET NULL"))
    event_type: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, server_default="{}")
