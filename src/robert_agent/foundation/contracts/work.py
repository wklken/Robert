"""Stable work values; member values are existing storage and protocol strings."""

from enum import StrEnum, unique


@unique
class DispatchRequestStatus(StrEnum):
    DRAFT = "draft"
    ACCEPTED = "accepted"
    CREATING = "creating"
    ISSUE_CREATED = "issue_created"
    ADMITTED = "admitted"
    DELIVERED = "delivered"
    FAILED = "failed"
    ATTENTION = "attention"


@unique
class TaskLifecycle(StrEnum):
    DETECTED = "detected"
    AUTHORIZED = "authorized"
    CLASSIFIED = "classified"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    WAITING_FOR_USER = "waiting_for_user"
    FAILED = "failed"
    CANCELED = "canceled"
    IGNORED = "ignored"


@unique
class WorkstreamLifecycle(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    WAITING_FOR_USER = "waiting_for_user"
    FAILED = "failed"
    CANCELED = "canceled"


@unique
class TaskKind(StrEnum):
    HUMAN_REQUEST = "human_request"
    CI_REMEDIATION = "ci_remediation"
    MERGE_CONFLICT_REMEDIATION = "merge_conflict_remediation"


@unique
class RemediationStatus(StrEnum):
    OPEN = "open"
    QUEUED = "queued"
    REMEDIATING = "remediating"
    WAITING_FOR_SIGNAL = "waiting_for_signal"
    RESOLVED = "resolved"
    EXHAUSTED = "exhausted"
    SUPERSEDED = "superseded"
    CANCELED = "canceled"


@unique
class WakeupStatus(StrEnum):
    PENDING = "pending"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    CANCELED = "canceled"


@unique
class FactClassification(StrEnum):
    ADDITIVE = "additive"
    INVALIDATING = "invalidating"
    TERMINAL = "terminal"
    IRRELEVANT = "irrelevant"


@unique
class WorkCommand(StrEnum):
    EDIT = "edit"
    START = "start"
    APPROVE = "approve"
    REPLY = "reply"
    REQUEST_CHANGES = "request_changes"
    RETRY = "retry"
    RETRY_PUBLISH = "retry_publish"
    CANCEL = "cancel"
    REOPEN = "reopen"


ACTIVE_TASK_LIFECYCLES = frozenset(
    {
        TaskLifecycle.DETECTED.value,
        TaskLifecycle.AUTHORIZED.value,
        TaskLifecycle.CLASSIFIED.value,
        TaskLifecycle.QUEUED.value,
        TaskLifecycle.RUNNING.value,
    }
)


TERMINAL_TASK_LIFECYCLES = frozenset(
    {
        TaskLifecycle.COMPLETED.value,
        TaskLifecycle.FAILED.value,
        TaskLifecycle.CANCELED.value,
        TaskLifecycle.IGNORED.value,
    }
)


LIVE_TASK_LIFECYCLES = ACTIVE_TASK_LIFECYCLES | {TaskLifecycle.WAITING_FOR_USER.value}


ACTIVE_REMEDIATION_STATUSES = frozenset(
    {
        RemediationStatus.OPEN.value,
        RemediationStatus.QUEUED.value,
        RemediationStatus.REMEDIATING.value,
        RemediationStatus.WAITING_FOR_SIGNAL.value,
    }
)


TERMINAL_REMEDIATION_STATUSES = frozenset(
    {
        RemediationStatus.RESOLVED.value,
        RemediationStatus.EXHAUSTED.value,
        RemediationStatus.SUPERSEDED.value,
        RemediationStatus.CANCELED.value,
    }
)


REMEDIATION_TASK_KINDS = frozenset(
    {
        TaskKind.CI_REMEDIATION.value,
        TaskKind.MERGE_CONFLICT_REMEDIATION.value,
    }
)


# Manual operator wakeups are independent of supervision recovery actions.
PUBLICATION_RETRY_WAKEUP_KIND = "publication_retry"
