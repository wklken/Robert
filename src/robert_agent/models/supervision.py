"""Supervision checkpoints, incidents and recovery actions."""

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


class SupervisionCheckpoint(Base):
    __tablename__ = "supervision_checkpoints"

    scope_type: Mapped[str] = mapped_column(Text, primary_key=True)
    scope_id: Mapped[str] = mapped_column(Text, primary_key=True)
    rule_id: Mapped[str] = mapped_column(Text, primary_key=True)
    lineage_key: Mapped[str] = mapped_column(Text, server_default="")
    classification: Mapped[str | None] = mapped_column(Text)
    reason_code: Mapped[str | None] = mapped_column(Text)
    evidence_json: Mapped[str] = mapped_column(Text, server_default="{}")
    progress_token: Mapped[str | None] = mapped_column(Text)
    last_progress_at: Mapped[str | None] = mapped_column(Text)
    eligible_since: Mapped[str | None] = mapped_column(Text)
    next_check_at: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        CheckConstraint("classification IN ('healthy','expected_wait','suspect','confirmed','unobservable')"),
        Index(
            "idx_supervision_checkpoints_due",
            "next_check_at",
            "scope_type",
            "scope_id",
            "rule_id",
            sqlite_where=text("next_check_at IS NOT NULL"),
        ),
    )


class SupervisionIncident(Base):
    __tablename__ = "supervision_incidents"

    incident_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    scope_type: Mapped[str] = mapped_column(Text)
    scope_id: Mapped[str] = mapped_column(Text)
    rule_id: Mapped[str] = mapped_column(Text)
    lineage_key: Mapped[str] = mapped_column(Text, server_default="")
    status: Mapped[str] = mapped_column(Text)
    reason_code: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[str] = mapped_column(Text)
    expected_progress_json: Mapped[str] = mapped_column(Text)
    first_observed_at: Mapped[str] = mapped_column(Text)
    last_observed_at: Mapped[str] = mapped_column(Text)
    next_check_at: Mapped[str | None] = mapped_column(Text)
    prior_incident_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("supervision_incidents.incident_id", ondelete="SET NULL"),
    )
    resolved_at: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[str | None] = mapped_column(Text)
    resolution_evidence_json: Mapped[str] = mapped_column(Text, server_default="{}")
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        CheckConstraint(
            "status IN ('open','diagnosing','recovering','verifying','needs_attention','resolved','dismissed')"
        ),
        Index(
            "idx_supervision_incidents_active",
            "scope_type",
            "scope_id",
            "rule_id",
            "lineage_key",
            unique=True,
            sqlite_where=text("status NOT IN ('resolved','dismissed')"),
        ),
        Index(
            "idx_supervision_incidents_recency",
            text("last_observed_at DESC"),
            text("incident_id DESC"),
        ),
        Index(
            "idx_supervision_incidents_scope_history",
            "scope_type",
            "scope_id",
            "rule_id",
            "lineage_key",
            text("first_observed_at DESC"),
        ),
    )


class SupervisionRecoveryAction(Base):
    __tablename__ = "supervision_recovery_actions"

    action_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    incident_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("supervision_incidents.incident_id"),
    )
    lineage_key: Mapped[str] = mapped_column(Text)
    scope_type: Mapped[str] = mapped_column(Text)
    scope_id: Mapped[str] = mapped_column(Text)
    stage_family: Mapped[str] = mapped_column(Text)
    action_kind: Mapped[str] = mapped_column(Text)
    recovery_ordinal: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    target_id: Mapped[str] = mapped_column(Text)
    target_generation: Mapped[str] = mapped_column(Text)
    policy_version: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    budget_cost: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    claim_owner: Mapped[str | None] = mapped_column(Text)
    claim_generation: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    claim_expires_at: Mapped[str | None] = mapped_column(Text)
    preconditions_json: Mapped[str] = mapped_column(Text, server_default="{}")
    expected_progress_json: Mapped[str] = mapped_column(Text, server_default="{}")
    receipt_json: Mapped[str] = mapped_column(Text, server_default="{}")
    verification_evidence_json: Mapped[str] = mapped_column(Text, server_default="{}")
    created_at: Mapped[str] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)
    next_check_at: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("recovery_ordinal > 0"),
        CheckConstraint(
            "status IN ('proposed','requested','applying','observing','succeeded','failed','rejected','unknown')"
        ),
        CheckConstraint("budget_cost >= 0"),
        CheckConstraint("claim_generation >= 0"),
        UniqueConstraint("lineage_key", "scope_type", "scope_id", "action_kind", "recovery_ordinal"),
        Index(
            "idx_supervision_recovery_active_scope",
            "scope_type",
            "scope_id",
            unique=True,
            sqlite_where=text("status IN ('requested','applying','observing','unknown')"),
        ),
        Index("idx_supervision_recovery_budget", "lineage_key", "stage_family"),
        Index("idx_supervision_recovery_due", "status", "next_check_at"),
    )
