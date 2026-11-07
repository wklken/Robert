"""Stable supervision values; member values are existing storage and protocol strings."""

from enum import StrEnum, unique


@unique
class IncidentStatus(StrEnum):
    OPEN = "open"
    DIAGNOSING = "diagnosing"
    RECOVERING = "recovering"
    VERIFYING = "verifying"
    NEEDS_ATTENTION = "needs_attention"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


@unique
class RecoveryStatus(StrEnum):
    PROPOSED = "proposed"
    REQUESTED = "requested"
    APPLYING = "applying"
    OBSERVING = "observing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@unique
class RecoveryActionKind(StrEnum):
    SPOOL_REPLAY = "spool_replay"
    AUDIT_RETRY = "audit_retry"
    HANDOFF_RESUME = "handoff_resume"
    PUBLICATION_RETRY = "publication_retry"
    PUBLICATION_RECONCILE = "publication_reconcile"
