"""Command input values and validation, independent of persistence and execution."""

from robert_agent.foundation.contracts.work import WorkCommand

from dataclasses import dataclass
from robert_agent.work.errors import WorkItemValidationError


MAX_TITLE = 200


MAX_DESCRIPTION = 20_000


MAX_REPLY = 8_000


MAX_IDEMPOTENCY_KEY = 128


PRIORITIES = frozenset({"P0", "P1", "P2", "P3"})


COMMANDS = frozenset(command.value for command in WorkCommand)


QUESTION_EVENTS = frozenset({"operator_question", "completion_acceptance_requested"})


DECISION_EVENTS = frozenset({"operator_decision_required"})


FAILURE_EVENTS = frozenset(
    {
        "execution_failed",
        "publication_failed",
        "manual_worker_unavailable",
        "review_attention",
        "unmerged_pr_closed",
    }
)


@dataclass(frozen=True)
class CommandContext:
    actor_kind: str
    actor_identity: str
    allowed_repo_ids: frozenset[str]
    allowed_workers: frozenset[str]


def validate_idempotency_key(value):
    if not isinstance(value, str) or not value.strip():
        raise WorkItemValidationError("idempotency_key is required")
    if len(value) > MAX_IDEMPOTENCY_KEY:
        raise WorkItemValidationError("idempotency_key is too long")
    return value


def validate_text(value, field, maximum, *, required=False):
    if not isinstance(value, str):
        raise WorkItemValidationError(f"{field} must be a string")
    value = value.strip() if required else value
    if required and not value:
        raise WorkItemValidationError(f"{field} is required")
    if len(value) > maximum:
        raise WorkItemValidationError(f"{field} is too long")
    return value


def validate_priority(priority):
    if priority not in PRIORITIES:
        raise WorkItemValidationError("priority must be one of P0, P1, P2, P3")
    return priority


def validate_routing(context, routing_mode, requested_worker):
    if routing_mode not in {"auto", "manual"}:
        raise WorkItemValidationError("routing_mode must be auto or manual")
    if routing_mode == "auto":
        if requested_worker is not None:
            raise WorkItemValidationError("automatic routing cannot select a worker")
        return routing_mode, None
    requested_worker = validate_text(
        requested_worker,
        "requested_worker",
        MAX_TITLE,
        required=True,
    )
    if requested_worker not in context.allowed_workers:
        raise WorkItemValidationError("requested worker is not available")
    return routing_mode, requested_worker
