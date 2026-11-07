"""Schema markers and historical workflow handoff records."""

from sqlalchemy import CheckConstraint, Index, Integer, Text, text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from robert_agent.models.base import Base


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    checksum: Mapped[str] = mapped_column(Text)
    applied_at: Mapped[str] = mapped_column(Text)


class WorkflowBinding(Base):
    __tablename__ = "workflow_bindings"

    subject_type: Mapped[str] = mapped_column(Text, primary_key=True)
    subject_id: Mapped[str] = mapped_column(Text, primary_key=True)
    workflow_kind: Mapped[str] = mapped_column(Text)
    owner_engine: Mapped[str] = mapped_column(Text)
    definition_version: Mapped[str] = mapped_column(Text, server_default="v1")
    workflow_id: Mapped[str | None] = mapped_column(Text, unique=True)
    shadow_workflow_id: Mapped[str | None] = mapped_column(Text, unique=True)
    binding_status: Mapped[str] = mapped_column(Text)
    recovery_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
    terminal_at: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        CheckConstraint("subject_type IN ('task', 'remediation_episode')"),
        CheckConstraint("workflow_kind IN ('human_request', 'pr_remediation')"),
        CheckConstraint("owner_engine IN ('legacy', 'dbos')"),
        CheckConstraint("binding_status IN ('active', 'terminal', 'attention')"),
        CheckConstraint("recovery_attempts BETWEEN 0 AND 1"),
        CheckConstraint(
            "(owner_engine = 'legacy' AND workflow_id IS NULL) OR (owner_engine = 'dbos' AND workflow_id IS NOT NULL)"
        ),
        Index(
            "idx_workflow_bindings_active",
            "owner_engine",
            "subject_type",
            "updated_at",
            sqlite_where=text("binding_status = 'active'"),
        ),
    )


class WorkflowOutbox(Base):
    __tablename__ = "workflow_outbox"

    signal_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    workflow_id: Mapped[str] = mapped_column(Text)
    topic: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    not_before_at: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    delivered_at: Mapped[str | None] = mapped_column(Text)
    last_error_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("status IN ('pending', 'delivered', 'attention')"),
        CheckConstraint("attempt_count >= 0"),
        UniqueConstraint("workflow_id", "topic", "dedupe_key"),
        Index(
            "idx_workflow_outbox_pending_due",
            "not_before_at",
            "created_at",
            sqlite_where=text("status = 'pending'"),
        ),
    )


class WorkflowSignalReceipt(Base):
    __tablename__ = "workflow_signal_receipts"

    signal_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    workflow_id: Mapped[str] = mapped_column(Text)
    topic: Mapped[str] = mapped_column(Text)
    consumed_at: Mapped[str] = mapped_column(Text)


class WorkflowShadowObservation(Base):
    __tablename__ = "workflow_shadow_observations"

    observation_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    subject_type: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[str] = mapped_column(Text)
    legacy_decision_json: Mapped[str] = mapped_column(Text)
    shadow_decision_json: Mapped[str] = mapped_column(Text)
    matched: Mapped[int] = mapped_column(Integer)
    mismatch_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("matched IN (0, 1)"),
        Index(
            "idx_workflow_shadow_observations_mismatch",
            "created_at",
            sqlite_where=text("matched = 0"),
        ),
    )
