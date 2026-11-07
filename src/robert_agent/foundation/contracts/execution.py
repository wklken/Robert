"""Stable execution values; member values are existing storage and protocol strings."""

from enum import StrEnum, unique


@unique
class AttemptStatus(StrEnum):
    PREPARED = "prepared"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STALE = "stale"
    CANCELED = "canceled"


@unique
class RoundStatus(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    REVIEWING = "reviewing"
    VOTING = "voting"
    SYNTHESIZING = "synthesizing"
    READY = "ready"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELED = "canceled"
    SUPERSEDED = "superseded"
    ATTENTION = "attention"
    WAITING_FOR_USER = "waiting_for_user"


@unique
class ModelRunStatus(StrEnum):
    PREPARED = "prepared"
    WAITING = "waiting"
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    ABANDONED = "abandoned"
    SUPERSEDED = "superseded"


@unique
class BindingStatus(StrEnum):
    ACTIVE = "active"
    TERMINAL = "terminal"
    ATTENTION = "attention"


@unique
class WorkerPhase(StrEnum):
    PREPARE = "prepare"
    ANALYZE = "analyze"
    PLAN = "plan"
    EXECUTE = "execute"
    VERIFY = "verify"
    PUBLISH = "publish"
    HANDOFF = "handoff"


RUNNING_ATTEMPT_STATUSES = frozenset(
    {
        AttemptStatus.RUNNING.value,
        AttemptStatus.STALE.value,
    }
)
