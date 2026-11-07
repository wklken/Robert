"""Worker results, publication records, artifacts and notifications."""

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


class WorkerResult(Base):
    __tablename__ = "worker_results"

    result_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    task_id: Mapped[str] = mapped_column(Text, ForeignKey("tasks.task_id", ondelete="CASCADE"))
    attempt_id: Mapped[str] = mapped_column(Text, ForeignKey("attempts.attempt_id", ondelete="CASCADE"))
    output_type: Mapped[str] = mapped_column(Text)
    consumed_event_fingerprints_json: Mapped[str] = mapped_column(Text, server_default="[]")
    verification_json: Mapped[str] = mapped_column(Text, server_default="[]")
    handoff: Mapped[str] = mapped_column(Text, server_default="")
    created_at: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (UniqueConstraint("task_id", "attempt_id"),)


class GitHubAction(Base):
    __tablename__ = "github_actions"

    action_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    result_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("worker_results.result_id", ondelete="SET NULL"),
    )
    task_id: Mapped[str] = mapped_column(Text, ForeignKey("tasks.task_id", ondelete="CASCADE"))
    action_type: Mapped[str] = mapped_column(Text)
    target_url: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    audit_status: Mapped[str] = mapped_column(Text, server_default="pending")
    publish_status: Mapped[str] = mapped_column(Text, server_default="not_published")
    created_at: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        CheckConstraint("audit_status IN ('pending', 'accepted', 'policy_violation', 'failed')"),
        CheckConstraint("publish_status IN ('not_published', 'published', 'skipped')"),
    )


class Artifact(Base):
    __tablename__ = "artifacts"

    artifact_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    task_id: Mapped[str | None] = mapped_column(Text, ForeignKey("tasks.task_id", ondelete="CASCADE"))
    attempt_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("attempts.attempt_id", ondelete="SET NULL"),
    )
    artifact_type: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(Text)
    bytes: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")


class Notification(Base):
    __tablename__ = "notifications"

    notification_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    task_id: Mapped[str | None] = mapped_column(Text, ForeignKey("tasks.task_id", ondelete="SET NULL"))
    notification_type: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text, server_default="local")
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        Index(
            "idx_notifications_delivery_pending",
            "channel",
            "status",
            "created_at",
            "notification_id",
            sqlite_where=text(
                "COALESCE(json_extract(metadata_json, '$.delivery.status'), '') NOT IN ('sent', 'exhausted')"
            ),
        ),
        Index(
            "idx_notifications_delivery_state",
            text("json_extract(metadata_json, '$.delivery.status')"),
        ),
    )


class PublicationIntent(Base):
    __tablename__ = "publication_intents"

    publication_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    action_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("github_actions.action_id", ondelete="CASCADE"),
        unique=True,
    )
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    request_fingerprint: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)
    remote_evidence_json: Mapped[str] = mapped_column(Text, server_default="{}")
    error_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ( 'prepared', 'executing', 'published', 'confirmed_absent', 'failed', 'attention' )"
        ),
    )
