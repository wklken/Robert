"""Attempts, leases and durable execution facts."""

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    text,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from robert_agent.models.base import Base


class Attempt(Base):
    __tablename__ = "attempts"

    attempt_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    task_id: Mapped[str] = mapped_column(Text, ForeignKey("tasks.task_id", ondelete="CASCADE"))
    attempt_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)
    worktree_path: Mapped[str | None] = mapped_column(Text)
    branch_name: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(Text)
    heartbeat_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)
    failure_json: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        CheckConstraint("attempt_no >= 1"),
        CheckConstraint("status IN ('prepared', 'running', 'completed', 'failed', 'stale', 'canceled')"),
        UniqueConstraint("task_id", "attempt_no"),
    )


class Lease(Base):
    __tablename__ = "leases"

    lease_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    resource_type: Mapped[str] = mapped_column(Text)
    resource_key: Mapped[str] = mapped_column(Text)
    owner_id: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    acquired_at: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[str] = mapped_column(Text)
    heartbeat_at: Mapped[str] = mapped_column(Text)
    released_at: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        CheckConstraint("status IN ('active', 'released', 'expired', 'stolen')"),
        Index("idx_leases_expired_retention", "status", text("julianday(expires_at)")),
        Index(
            "idx_leases_one_active_resource",
            "resource_type",
            "resource_key",
            unique=True,
            sqlite_where=text("status = 'active'"),
        ),
        Index("idx_leases_owner_status", "owner_id", "status"),
        Index("idx_leases_released_retention", "status", text("julianday(released_at)")),
    )


class WorkerPhase(Base):
    __tablename__ = "worker_phases"

    phase_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    attempt_id: Mapped[str] = mapped_column(Text, ForeignKey("attempts.attempt_id", ondelete="CASCADE"))
    phase: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    next_step: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        CheckConstraint("phase IN ('prepare', 'analyze', 'plan', 'execute', 'verify', 'publish', 'handoff')"),
        Index(
            "idx_worker_phases_attempt_created",
            "attempt_id",
            text("created_at DESC"),
            text("phase_id DESC"),
        ),
    )


class Wakeup(Base):
    __tablename__ = "wakeups"

    wakeup_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    repo_id: Mapped[str] = mapped_column(Text, ForeignKey("repos.repo_id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(Text)
    work_item_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("work_items.work_item_id", ondelete="CASCADE"),
    )
    task_id: Mapped[str | None] = mapped_column(Text, ForeignKey("tasks.task_id", ondelete="CASCADE"))
    attempt_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("attempts.attempt_id", ondelete="SET NULL"),
    )
    result_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("worker_results.result_id", ondelete="CASCADE"),
    )
    source_run_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("agent_runs.run_id", ondelete="SET NULL"),
    )
    consumed_run_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("agent_runs.run_id", ondelete="SET NULL"),
    )
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    not_before_at: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        CheckConstraint(
            "reason IN ( 'worker_result_ready', 'queued_capacity_wait', 'publish_retry_ready', "
            "'stale_attempt_check', 'manual_operator_request' )"
        ),
        CheckConstraint("status IN ('pending', 'consumed', 'expired', 'canceled')"),
        CheckConstraint("attempt_count >= 0"),
        UniqueConstraint("repo_id", "reason", "dedupe_key"),
        Index("idx_wakeups_consumed_run", "consumed_run_id"),
        Index(
            "idx_wakeups_pending_work_item",
            "work_item_id",
            "status",
            "not_before_at",
            sqlite_where=text("work_item_id IS NOT NULL"),
        ),
        Index("idx_wakeups_result", "result_id"),
        Index("idx_wakeups_source_run", "source_run_id"),
        Index("idx_wakeups_status_due", "repo_id", "status", "not_before_at", "created_at"),
    )


class ExecutionSubject(Base):
    __tablename__ = "execution_subjects"

    subject_type: Mapped[str] = mapped_column(Text, primary_key=True)
    subject_id: Mapped[str] = mapped_column(Text, primary_key=True)
    workflow_kind: Mapped[str] = mapped_column(Text)
    binding_status: Mapped[str] = mapped_column(Text)
    recovery_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
    terminal_at: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        CheckConstraint("subject_type IN ('task','remediation_episode')"),
        CheckConstraint("workflow_kind IN ('human_request','pr_remediation')"),
        CheckConstraint("binding_status IN ('active','terminal','attention')"),
        CheckConstraint("recovery_attempts BETWEEN 0 AND 1"),
    )


class ExecutionPendingFact(Base):
    __tablename__ = "execution_pending_facts"

    source_signal_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    subject_type: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[str] = mapped_column(Text)
    topic: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(Text)
    fact_id: Mapped[str] = mapped_column(Text)
    disposition: Mapped[str] = mapped_column(Text)
    repo_id: Mapped[str] = mapped_column(Text)
    task_id: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text)
    not_before_at: Mapped[str] = mapped_column(Text)
    consumed_at: Mapped[str | None] = mapped_column(Text)
    resolution_json: Mapped[str] = mapped_column(Text, server_default="{}")


class ExecutionFactReceipt(Base):
    __tablename__ = "execution_fact_receipts"

    subject_type: Mapped[str] = mapped_column(Text, primary_key=True)
    subject_id: Mapped[str] = mapped_column(Text, primary_key=True)
    fact_id: Mapped[str] = mapped_column(Text, primary_key=True)
    evidence_json: Mapped[str | None] = mapped_column(Text)


class ExecutionMigration(Base):
    __tablename__ = "execution_migrations"

    migration_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    source_fingerprint: Mapped[str] = mapped_column(Text, unique=True)
    applied_at: Mapped[str] = mapped_column(Text)
    snapshot_json: Mapped[str] = mapped_column(Text)
    mappings_json: Mapped[str] = mapped_column(Text)
