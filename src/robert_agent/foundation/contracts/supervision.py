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


@unique
class SupervisionMode(StrEnum):
    OFF = "off"
    OBSERVE = "observe"
    RECOVER_KNOWN = "recover_known"
    DECIDE = "decide"


@unique
class DecisionAction(StrEnum):
    REPAIR_CI = "repair_ci"
    RESOLVE_CONFLICT = "resolve_conflict"
    RETRY_WORKER = "retry_worker"
    SPOOL_REPLAY = RecoveryActionKind.SPOOL_REPLAY.value
    AUDIT_RETRY = RecoveryActionKind.AUDIT_RETRY.value
    HANDOFF_RESUME = RecoveryActionKind.HANDOFF_RESUME.value
    PUBLICATION_RETRY = RecoveryActionKind.PUBLICATION_RETRY.value
    PUBLICATION_RECONCILE = RecoveryActionKind.PUBLICATION_RECONCILE.value
    WAIT = "wait"
    NOTIFY_USER = "notify_user"
    ASK_USER = "ask_user"
    FINISH_SUPERVISION = "finish_supervision"


@unique
class DecisionStatus(StrEnum):
    APPROVED = "approved"
    VERIFYING = "verifying"
    WAIT = "wait"
    NOTIFY_USER = "notify_user"
    ASK_USER = "ask_user"
    FINISH_SUPERVISION = "finish_supervision"
    REPLY_RECEIVED = "reply_received"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    STALE_EVIDENCE = "stale_evidence"
    EXITED = "exited"
    INVALID_OUTPUT = "invalid_output"
    TIMED_OUT = "timed_out"
    STOPPED = "stopped"
    LAUNCH_FAILED = "launch_failed"
    PROCESS_ATTENTION = "process_attention"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"


@unique
class DecisionRunStatus(StrEnum):
    DISABLED = "disabled"
    IDLE = "idle"
    COOLDOWN = "cooldown"
    CALL_BUDGET_EXHAUSTED = "call_budget_exhausted"
    LAUNCHED = "launched"
    RUNNING = "running"
    DECIDED = "decided"
    CLAIM_CHANGED = "claim_changed"
    PREVIOUS_PROCESS_RUNNING = "previous_process_running"
    INTERRUPTED = "interrupted"
    UNAVAILABLE = "unavailable"
    OPERATOR_RESOLVED = "operator_resolved"
    STALE_EVIDENCE = "stale_evidence"
    EXITED = "exited"
    INVALID_OUTPUT = "invalid_output"
    TIMED_OUT = "timed_out"
    STOPPED = "stopped"
    LAUNCH_FAILED = "launch_failed"
    PROCESS_ATTENTION = "process_attention"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"


NONEXECUTING_DECISION_ACTIONS = frozenset(
    {
        DecisionAction.WAIT.value,
        DecisionAction.NOTIFY_USER.value,
        DecisionAction.ASK_USER.value,
        DecisionAction.FINISH_SUPERVISION.value,
    }
)
AUTOMATIC_DECISION_ACTIONS = frozenset(action.value for action in DecisionAction) - NONEXECUTING_DECISION_ACTIONS
