"""Review and vote rounds and their shared model process pool."""

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


class ReviewSession(Base):
    __tablename__ = "review_sessions"

    round_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    repo: Mapped[str] = mapped_column(Text)
    pr_number: Mapped[int] = mapped_column(Integer)
    workstream_id: Mapped[str] = mapped_column(Text)
    trigger_fingerprint: Mapped[str] = mapped_column(Text)
    task_id: Mapped[str | None] = mapped_column(Text, ForeignKey("tasks.task_id"), unique=True)
    status: Mapped[str] = mapped_column(Text, server_default="queued")
    request_json: Mapped[str] = mapped_column(Text)
    manifest_json: Mapped[str | None] = mapped_column(Text)
    report_path: Mapped[str | None] = mapped_column(Text)
    report_sha256: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("repo", "pr_number", "trigger_fingerprint"),
        Index("review_sessions_workstream", "workstream_id", "created_at"),
    )


class ReviewRun(Base):
    __tablename__ = "review_runs"

    run_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    round_id: Mapped[str] = mapped_column(Text, ForeignKey("review_sessions.round_id"))
    stage: Mapped[str] = mapped_column(Text)
    worker_id: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    attempt_no: Mapped[int] = mapped_column(Integer)
    owner_attempt_id: Mapped[str] = mapped_column(Text, ForeignKey("attempts.attempt_id"))
    status: Mapped[str] = mapped_column(Text)
    output_dir: Mapped[str] = mapped_column(Text, unique=True)
    prompt_sha256: Mapped[str] = mapped_column(Text)
    slot_reserved: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    started_at: Mapped[str | None] = mapped_column(Text)
    dispatch_json: Mapped[str | None] = mapped_column(Text)
    artifact_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("stage IN ('prepare','review','synthesis')"),
        UniqueConstraint("round_id", "stage", "worker_id", "attempt_no"),
        Index(
            "review_runs_reserved",
            "slot_reserved",
            sqlite_where=text("slot_reserved=1"),
        ),
    )


class VoteSession(Base):
    __tablename__ = "vote_sessions"

    round_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    repo: Mapped[str] = mapped_column(Text)
    number: Mapped[int] = mapped_column(Integer)
    source_type: Mapped[str] = mapped_column(Text)
    workstream_id: Mapped[str] = mapped_column(Text)
    trigger_fingerprint: Mapped[str] = mapped_column(Text)
    task_id: Mapped[str | None] = mapped_column(Text, ForeignKey("tasks.task_id"), unique=True)
    status: Mapped[str] = mapped_column(Text, server_default="queued")
    request_json: Mapped[str] = mapped_column(Text)
    manifest_json: Mapped[str | None] = mapped_column(Text)
    report_path: Mapped[str | None] = mapped_column(Text)
    report_sha256: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("repo", "source_type", "number", "trigger_fingerprint"),
        Index("vote_sessions_workstream", "workstream_id", "created_at"),
    )


class VoteRun(Base):
    __tablename__ = "vote_runs"

    run_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    round_id: Mapped[str] = mapped_column(Text, ForeignKey("vote_sessions.round_id"))
    stage: Mapped[str] = mapped_column(Text)
    worker_id: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    attempt_no: Mapped[int] = mapped_column(Integer)
    owner_attempt_id: Mapped[str] = mapped_column(Text, ForeignKey("attempts.attempt_id"))
    status: Mapped[str] = mapped_column(Text)
    output_dir: Mapped[str] = mapped_column(Text, unique=True)
    prompt_sha256: Mapped[str] = mapped_column(Text)
    slot_reserved: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    started_at: Mapped[str | None] = mapped_column(Text)
    dispatch_json: Mapped[str | None] = mapped_column(Text)
    artifact_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("stage IN ('prepare','vote','synthesis')"),
        UniqueConstraint("round_id", "stage", "worker_id", "attempt_no"),
        Index("vote_runs_reserved", "slot_reserved", sqlite_where=text("slot_reserved=1")),
    )


class ReviewProcessPool(Base):
    __tablename__ = "review_process_pool"

    singleton: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=True)
    max_concurrency: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint("singleton=1"),
        CheckConstraint("max_concurrency>0"),
    )
