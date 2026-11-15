"""Worker results, publication records, artifacts and notifications."""

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
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

    __table_args__ = (
        Index("idx_worker_results_recent", text("created_at DESC"), text("result_id DESC")),
        UniqueConstraint("task_id", "attempt_id"),
    )


class GitHubAction(Base):
    __tablename__ = "github_actions"

    action_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    result_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("worker_results.result_id", ondelete="SET NULL"),
    )
    task_id: Mapped[str | None] = mapped_column(Text, ForeignKey("tasks.task_id", ondelete="CASCADE"))
    source_result_id: Mapped[str | None] = mapped_column(Text, ForeignKey("source_results.source_result_id"))
    action_type: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text, server_default="github")
    target_json: Mapped[str | None] = mapped_column(Text)
    target_url: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    audit_status: Mapped[str] = mapped_column(Text, server_default="pending")
    publish_status: Mapped[str] = mapped_column(Text, server_default="not_published")
    created_at: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        Index("idx_github_actions_recent", text("created_at DESC"), text("action_id DESC")),
        CheckConstraint("(task_id IS NOT NULL) != (source_result_id IS NOT NULL)"),
        CheckConstraint(
            "task_id IS NOT NULL OR (result_id IS NULL AND target_json IS NOT NULL AND "
            "((provider = 'tapd' AND action_type = 'comment') OR (provider = 'github' AND action_type = 'source_feedback')))"
        ),
        CheckConstraint("provider IN ('github', 'tapd') AND (provider = 'github' OR target_json IS NOT NULL)"),
        CheckConstraint("audit_status IN ('pending', 'accepted', 'policy_violation', 'failed')"),
        CheckConstraint("publish_status IN ('not_published', 'published', 'skipped')"),
    )


class SourceResult(Base):
    __tablename__ = "source_results"

    source_result_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, ForeignKey("sources.source_id"))
    round_id: Mapped[str | None] = mapped_column(Text, ForeignKey("source_rounds.round_id"), unique=True)
    kind: Mapped[str] = mapped_column(Text)
    input_version: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    worker_result_id: Mapped[str | None] = mapped_column(Text, ForeignKey("worker_results.result_id"), unique=True)
    model_run_id: Mapped[str | None] = mapped_column(Text, ForeignKey("source_model_runs.run_id"), unique=True)
    evidence_json: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text)
    accepted_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("kind IN ('answer','question','control_notice')"),
        CheckConstraint(
            "(kind = 'control_notice' AND round_id IS NULL AND worker_result_id IS NULL AND model_run_id IS NULL) OR "
            "(kind != 'control_notice' AND round_id IS NOT NULL AND "
            "(worker_result_id IS NOT NULL) != (model_run_id IS NOT NULL))"
        ),
        Index("idx_source_results_source", "source_id", "accepted_at", "source_result_id"),
    )


class SourceResultRetirement(Base):
    __tablename__ = "source_result_retirements"

    source_result_id: Mapped[str] = mapped_column(Text, ForeignKey("source_results.source_result_id"), primary_key=True)
    observation_revision: Mapped[str] = mapped_column(Text)
    proof_json: Mapped[str] = mapped_column(Text)
    retired_at: Mapped[str] = mapped_column(Text)


class SourceRequiredAction(Base):
    __tablename__ = "source_required_actions"

    action_id: Mapped[str] = mapped_column(Text, ForeignKey("github_actions.action_id"), primary_key=True)
    source_result_id: Mapped[str] = mapped_column(Text, ForeignKey("source_results.source_result_id"))
    position: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("source_result_id", "position"),
        CheckConstraint("position >= 0"),
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
        Index("idx_notifications_recent", text("created_at DESC"), text("notification_id DESC")),
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


class SourceAttention(Base):
    __tablename__ = "source_attentions"

    notification_id: Mapped[str] = mapped_column(Text, ForeignKey("notifications.notification_id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, ForeignKey("source_sessions.source_id"))
    round_id: Mapped[str] = mapped_column(Text, ForeignKey("source_rounds.round_id"))
    kind: Mapped[str] = mapped_column(Text)
    cause: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    repo_id: Mapped[str | None] = mapped_column(Text, ForeignKey("repos.repo_id"))
    repo_basis: Mapped[str | None] = mapped_column(Text)
    task_id: Mapped[str | None] = mapped_column(Text, ForeignKey("source_round_tasks.task_id"))
    attempt_id: Mapped[str | None] = mapped_column(Text, ForeignKey("attempts.attempt_id"))
    model_run_id: Mapped[str | None] = mapped_column(Text, ForeignKey("source_model_runs.run_id"))
    created_at: Mapped[str] = mapped_column(Text)
    resolved_at: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("version > 0"),
        CheckConstraint("(repo_id IS NULL) = (repo_basis IS NULL)"),
        CheckConstraint("repo_basis IN ('task', 'children', 'round', 'selection')"),
        CheckConstraint("(task_id IS NOT NULL) = (COALESCE(repo_basis, '') = 'task')"),
        CheckConstraint("attempt_id IS NULL OR task_id IS NOT NULL"),
        CheckConstraint("model_run_id IS NULL OR task_id IS NULL"),
        UniqueConstraint("source_id", "kind", "version"),
        Index(
            "idx_source_attentions_open_coordination",
            "source_id",
            unique=True,
            sqlite_where=text("kind = 'coordination' AND resolved_at IS NULL"),
        ),
        Index(
            "idx_source_attentions_open_recovery",
            "source_id",
            unique=True,
            sqlite_where=text("kind = 'delivery_recovery' AND resolved_at IS NULL"),
        ),
    )


class SourceQuestionAttention(Base):
    __tablename__ = "source_question_attentions"

    notification_id: Mapped[str] = mapped_column(
        Text, ForeignKey("source_attentions.notification_id"), primary_key=True
    )
    source_result_id: Mapped[str] = mapped_column(Text, ForeignKey("source_results.source_result_id"), unique=True)
    publication_id: Mapped[str] = mapped_column(Text, ForeignKey("publication_intents.publication_id"), unique=True)
    body: Mapped[str] = mapped_column(Text)
    result_evidence_json: Mapped[str] = mapped_column(Text)
    prepared_evidence_json: Mapped[str] = mapped_column(Text)
    remote_evidence_json: Mapped[str] = mapped_column(Text)
    intent_fingerprint: Mapped[str] = mapped_column(Text)


class SourceRecoveryAttention(Base):
    __tablename__ = "source_recovery_attentions"

    notification_id: Mapped[str] = mapped_column(
        Text, ForeignKey("source_attentions.notification_id"), primary_key=True
    )
    source_result_id: Mapped[str] = mapped_column(Text)
    generation: Mapped[int] = mapped_column(Integer)
    pause_proof_json: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        ForeignKeyConstraint(
            ["source_result_id", "generation"],
            ["source_delivery_pauses.source_result_id", "source_delivery_pauses.generation"],
        ),
    )


class SourcePublicationAttention(Base):
    __tablename__ = "source_publication_attentions"

    notification_id: Mapped[str] = mapped_column(Text, ForeignKey("notifications.notification_id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, ForeignKey("source_sessions.source_id"))
    source_result_id: Mapped[str] = mapped_column(Text, ForeignKey("source_results.source_result_id"))
    action_id: Mapped[str] = mapped_column(Text, ForeignKey("github_actions.action_id"))
    publication_id: Mapped[str] = mapped_column(Text, ForeignKey("publication_intents.publication_id"))
    request_fingerprint: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    cause: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(Text)
    resolved_at: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("version > 0"),
        CheckConstraint("provider IN ('tapd', 'github')"),
        UniqueConstraint("publication_id", "version"),
        Index(
            "idx_source_publication_attentions_open",
            "publication_id",
            unique=True,
            sqlite_where=text("resolved_at IS NULL"),
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


class TapdPublicationPreparation(Base):
    __tablename__ = "tapd_publication_preparations"

    publication_id: Mapped[str] = mapped_column(
        Text, ForeignKey("publication_intents.publication_id"), primary_key=True
    )
    prepared_evidence_json: Mapped[str] = mapped_column(Text)


class GitHubPublicationPreparation(Base):
    __tablename__ = "github_publication_preparations"

    publication_id: Mapped[str] = mapped_column(
        Text, ForeignKey("publication_intents.publication_id"), primary_key=True
    )
    prepared_evidence_json: Mapped[str] = mapped_column(Text)


class SourceConfirmationQuestion(Base):
    __tablename__ = "source_confirmation_questions"

    question_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, ForeignKey("sources.source_id"))
    version: Mapped[int] = mapped_column(Integer)
    scope_json: Mapped[str] = mapped_column(Text)
    scope_digest: Mapped[str] = mapped_column(Text)
    source_result_id: Mapped[str] = mapped_column(Text, ForeignKey("source_results.source_result_id"), unique=True)
    action_id: Mapped[str] = mapped_column(Text, ForeignKey("github_actions.action_id"), unique=True)
    created_at: Mapped[str] = mapped_column(Text)
    superseded_at: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("version > 0"),
        UniqueConstraint("source_id", "version"),
        Index("idx_source_confirmation_current", "source_id", unique=True, sqlite_where=text("superseded_at IS NULL")),
    )


class SourceConfirmationAnswer(Base):
    __tablename__ = "source_confirmation_answers"

    question_id: Mapped[str] = mapped_column(
        Text, ForeignKey("source_confirmation_questions.question_id"), primary_key=True
    )
    event_id: Mapped[str] = mapped_column(Text, ForeignKey("source_events.event_id"), unique=True)
    publication_id: Mapped[str] = mapped_column(Text, ForeignKey("publication_intents.publication_id"))
    comment_id: Mapped[str] = mapped_column(Text)
    proof_json: Mapped[str] = mapped_column(Text)
    accepted_at: Mapped[str] = mapped_column(Text)
    revoked_at: Mapped[str | None] = mapped_column(Text)
    consumer_id: Mapped[str | None] = mapped_column(Text)
    claimed_at: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (CheckConstraint("(consumer_id IS NULL) = (claimed_at IS NULL)"),)


class SourceResumeTransition(Base):
    __tablename__ = "source_resume_transitions"

    transition_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, ForeignKey("sources.source_id"))
    question_id: Mapped[str] = mapped_column(Text, ForeignKey("source_confirmation_answers.question_id"), unique=True)
    binding_request_id: Mapped[str] = mapped_column(Text, ForeignKey("ingress_requests.request_id"))
    paused_binding_json: Mapped[str] = mapped_column(Text)
    receipt_json: Mapped[str] = mapped_column(Text)
    repository_json: Mapped[str] = mapped_column(Text)
    request_ids_json: Mapped[str] = mapped_column(Text)
    applied_at: Mapped[str] = mapped_column(Text)


class SourceDeliveryPause(Base):
    __tablename__ = "source_delivery_pauses"

    source_result_id: Mapped[str] = mapped_column(Text, ForeignKey("source_results.source_result_id"), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, ForeignKey("sources.source_id"))
    round_id: Mapped[str] = mapped_column(Text, ForeignKey("source_round_cancellations.round_id"))
    observation_revision: Mapped[str] = mapped_column(Text)
    proof_json: Mapped[str] = mapped_column(Text)
    paused_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("generation > 0"),
        Index("idx_source_delivery_pauses_source", "source_id", "round_id"),
    )


class SourceDeliveryRecovery(Base):
    __tablename__ = "source_delivery_recoveries"

    source_result_id: Mapped[str] = mapped_column(Text, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, ForeignKey("sources.source_id"))
    question_id: Mapped[str] = mapped_column(Text, ForeignKey("source_confirmation_questions.question_id"), unique=True)
    transition_id: Mapped[str] = mapped_column(Text, unique=True)
    binding_sha256: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[str] = mapped_column(Text)
    claimed_at: Mapped[str] = mapped_column(Text)
    completed_at: Mapped[str | None] = mapped_column(Text)
    suspended_at: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        ForeignKeyConstraint(
            ["source_result_id", "generation"],
            ["source_delivery_pauses.source_result_id", "source_delivery_pauses.generation"],
        ),
        Index(
            "idx_source_delivery_recovery_active",
            "source_id",
            unique=True,
            sqlite_where=text("completed_at IS NULL AND suspended_at IS NULL"),
        ),
    )
