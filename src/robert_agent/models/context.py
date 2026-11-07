"""Project memory and runtime knowledge records."""

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


class ProjectMemoryEntry(Base):
    __tablename__ = "project_memory_entries"

    memory_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    repo_id: Mapped[str] = mapped_column(Text, ForeignKey("repos.repo_id", ondelete="CASCADE"))
    memory_thread_key: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, server_default="context")
    title: Mapped[str] = mapped_column(Text)
    short_summary: Mapped[str] = mapped_column(Text)
    long_summary: Mapped[str] = mapped_column(Text, server_default="")
    confidence: Mapped[str] = mapped_column(Text, server_default="medium")
    source_task_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("tasks.task_id", ondelete="SET NULL"),
    )
    source_result_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("worker_results.result_id", ondelete="SET NULL"),
    )
    current_revision_id: Mapped[str | None] = mapped_column(Text)
    revision_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
    last_used_at: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        CheckConstraint("confidence IN ('high', 'medium', 'low')"),
        CheckConstraint("revision_count >= 0"),
        UniqueConstraint("repo_id", "memory_thread_key", "kind", "title"),
        Index("idx_project_memory_entries_repo_updated", "repo_id", "updated_at"),
    )


class ProjectMemoryRevision(Base):
    __tablename__ = "project_memory_revisions"

    revision_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    memory_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("project_memory_entries.memory_id", ondelete="CASCADE"),
    )
    repo_id: Mapped[str] = mapped_column(Text, ForeignKey("repos.repo_id", ondelete="CASCADE"))
    result_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("worker_results.result_id", ondelete="SET NULL"),
    )
    task_id: Mapped[str | None] = mapped_column(Text, ForeignKey("tasks.task_id", ondelete="SET NULL"))
    attempt_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("attempts.attempt_id", ondelete="SET NULL"),
    )
    operation: Mapped[str] = mapped_column(Text, server_default="upsert")
    title: Mapped[str] = mapped_column(Text)
    short_summary: Mapped[str] = mapped_column(Text)
    long_summary: Mapped[str] = mapped_column(Text, server_default="")
    payload_json: Mapped[str] = mapped_column(Text, server_default="{}")
    created_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (Index("idx_project_memory_revisions_memory", "memory_id", "created_at"),)


class ProjectMemoryTerm(Base):
    __tablename__ = "project_memory_terms"

    memory_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("project_memory_entries.memory_id", ondelete="CASCADE"),
        primary_key=True,
    )
    repo_id: Mapped[str] = mapped_column(Text, ForeignKey("repos.repo_id", ondelete="CASCADE"))
    term_type: Mapped[str] = mapped_column(Text, primary_key=True)
    term_value: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (Index("idx_project_memory_terms_lookup", "repo_id", "term_type", "term_value"),)


class KnowledgeCandidate(Base):
    __tablename__ = "knowledge_candidates"

    candidate_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    repo_id: Mapped[str] = mapped_column(Text, ForeignKey("repos.repo_id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    prompt_text: Mapped[str] = mapped_column(Text)
    candidate_type: Mapped[str] = mapped_column(Text, server_default="rule")
    source_memory_ids_json: Mapped[str] = mapped_column(Text, server_default="[]")
    evidence_json: Mapped[str] = mapped_column(Text, server_default="[]")
    confidence: Mapped[str] = mapped_column(Text, server_default="medium")
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    created_at: Mapped[str] = mapped_column(Text)
    reviewed_at: Mapped[str | None] = mapped_column(Text)
    reviewer: Mapped[str | None] = mapped_column(Text)
    review_note: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        CheckConstraint("candidate_type IN ('rule', 'anti_pattern', 'route_hint', 'verification_hint')"),
        CheckConstraint("confidence IN ('high', 'medium', 'low')"),
        CheckConstraint("status IN ('pending', 'approved', 'rejected', 'superseded')"),
        UniqueConstraint("repo_id", "title", "source_memory_ids_json"),
        Index("idx_knowledge_candidates_status", "repo_id", "status", "created_at"),
    )


class RuntimeKnowledge(Base):
    __tablename__ = "runtime_knowledge"

    knowledge_id: Mapped[str | None] = mapped_column(Text, primary_key=True, nullable=True)
    candidate_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("knowledge_candidates.candidate_id", ondelete="SET NULL"),
    )
    repo_id: Mapped[str] = mapped_column(Text, ForeignKey("repos.repo_id", ondelete="CASCADE"))
    scope_type: Mapped[str] = mapped_column(Text)
    scope_value: Mapped[str] = mapped_column(Text, server_default="")
    title: Mapped[str] = mapped_column(Text)
    prompt_text: Mapped[str] = mapped_column(Text)
    retrieval_boost_json: Mapped[str] = mapped_column(Text, server_default="{}")
    active: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    approved_by: Mapped[str] = mapped_column(Text)
    approved_at: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, server_default="{}")

    __table_args__ = (
        CheckConstraint("scope_type IN ('global', 'route', 'path', 'symbol', 'workstream')"),
        CheckConstraint("active IN (0, 1)"),
        Index(
            "idx_runtime_knowledge_lookup",
            "repo_id",
            "active",
            "scope_type",
            "scope_value",
        ),
    )
