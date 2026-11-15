"""GitHub discovery cache, scan state and child records."""

from sqlalchemy import ForeignKey, Index, Integer, REAL, text, Text
from sqlalchemy.orm import Mapped, mapped_column

from robert_agent.models.base import Base


class GitHubReadCache(Base):
    __tablename__ = "github_read_cache"

    scope: Mapped[str] = mapped_column(Text, primary_key=True)
    cache_key: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[float] = mapped_column(REAL)

    __table_args__ = (Index("idx_github_read_cache_expiry", "expires_at"),)


class GitHubScanBatch(Base):
    __tablename__ = "github_scan_batches"

    batch_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    repo_id: Mapped[str] = mapped_column(Text)
    context_digest: Mapped[str] = mapped_column(Text)
    raw_events_json: Mapped[str] = mapped_column(Text)
    complete_sources_json: Mapped[str] = mapped_column(Text, server_default="[]")
    retry_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    attention_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(REAL)
    retry_at: Mapped[float] = mapped_column(REAL)
    completed_at: Mapped[float | None] = mapped_column(REAL)

    __table_args__ = (Index("idx_github_scan_batches_pending", "repo_id", "completed_at", "retry_at"),)


class GitHubControlChild(Base):
    __tablename__ = "github_control_children"

    child_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    owner_id: Mapped[str] = mapped_column(Text)
    lane: Mapped[str] = mapped_column(Text)
    identity_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)


class GitHubScanRead(Base):
    __tablename__ = "github_scan_reads"

    repo_id: Mapped[str] = mapped_column(Text, primary_key=True)
    command_key: Mapped[str] = mapped_column(Text, primary_key=True)
    response_json: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[float] = mapped_column(REAL)


class TapdDiscoveryProgress(Base):
    __tablename__ = "tapd_discovery_progress"

    probe_key: Mapped[str] = mapped_column(Text, primary_key=True)
    attempted_at: Mapped[str] = mapped_column(Text)
    completed_at: Mapped[str | None] = mapped_column(Text)
    attention_reason: Mapped[str | None] = mapped_column(Text)


class TapdObservationProgress(Base):
    __tablename__ = "tapd_observation_progress"

    source_id: Mapped[str] = mapped_column(Text, ForeignKey("sources.source_id", ondelete="CASCADE"), primary_key=True)
    attempted_at: Mapped[str] = mapped_column(Text)
    completed_at: Mapped[str | None] = mapped_column(Text)
    attention_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("idx_tapd_observation_progress_attempted", "attempted_at", "source_id"),)


class TapdCoordination(Base):
    __tablename__ = "tapd_coordination"

    source_id: Mapped[str] = mapped_column(Text, ForeignKey("sources.source_id", ondelete="CASCADE"), primary_key=True)
    updated_at: Mapped[str] = mapped_column(Text)
    next_due_at: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    round_id: Mapped[str | None] = mapped_column(Text, ForeignKey("source_rounds.round_id"))

    __table_args__ = (Index("idx_tapd_coordination_updated", "updated_at", "source_id"),)
