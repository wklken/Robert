"""Versioned Web decisions that durably enqueue an approved proposal."""

from contextlib import closing

from robert_agent.adapters.notifications import wakeup as notifier
from robert_agent.foundation import values
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import proposals as queries
from robert_agent.persistence.stores import proposals as stores
from robert_agent.security.text import safe_text
from robert_agent.work import wakeup
from robert_agent.work.errors import WorkItemConflictError, WorkItemNotFoundError, WorkItemValidationError
from robert_agent.work.inputs import validate_idempotency_key, validate_text


_ACTIONS = {"approve": "approved", "hold": "held", "discard": "discarded"}


def decide(db_path, proposal_id, *, context, action, expected_version, idempotency_key, reason=""):
    validate_idempotency_key(idempotency_key)
    reason = safe_text(validate_text(reason, "reason", 1500), 1500)
    if (
        not isinstance(action, str)
        or action not in _ACTIONS
        or type(expected_version) is not int
        or expected_version < 1
    ):
        raise WorkItemValidationError("invalid proposal decision or version")
    if context.actor_kind != "operator" or not context.actor_identity:
        raise WorkItemValidationError("proposal decisions require an operator identity")
    now = values.utc_now()
    with closing(database.connect(db_path)) as conn, conn:
        transactions.begin_immediate(conn)
        rows = queries.current(conn, proposal_id=proposal_id)
        if not rows or rows[0]["repo_id"] not in context.allowed_repo_ids:
            raise WorkItemNotFoundError("proposal not found")
        row = rows[0]
        previous = queries.get_decision(conn, proposal_id, idempotency_key=idempotency_key)
        if previous:
            if (
                previous["actor"] != context.actor_identity
                or previous["action"] != action
                or previous["version"] != expected_version + 1
                or previous["reason"] != reason
            ):
                raise WorkItemConflictError("idempotency key was used for a different proposal decision")
            return {key: value for key, value in row.items() if key not in {"source_json", "report_json"}}
        if row["version"] != expected_version:
            raise WorkItemConflictError("proposal version changed; refresh before deciding")
        if row["status"] not in {"pending", "held", "blocked"}:
            raise WorkItemConflictError("proposal has already been decided")
        if row["status"] == "blocked" and action != "discard":
            raise WorkItemConflictError("proposal needs refreshed evidence before approval")
        version = row["version"] + 1
        stores.record_decision(
            conn,
            {
                "proposal_id": proposal_id,
                "version": version,
                "revision": row["revision"],
                "action": action,
                "actor": context.actor_identity,
                "idempotency_key": idempotency_key,
                "reason": reason,
                "created_at": now,
            },
        )
        stores.revise(
            conn,
            proposal_id,
            row["version"],
            version=version,
            status=_ACTIONS[action],
            blocked_reason="",
            updated_at=now,
        )
        if action == "approve":
            wakeup.request_wakeup(
                conn,
                repo_id=row["repo_id"],
                reason="manual_operator_request",
                dedupe_key=f"proposal:{proposal_id}:{version}",
                metadata={"kind": "proposal_approved"},
                now=now,
            )
        result = {**row, "version": version, "status": _ACTIONS[action], "blocked_reason": "", "updated_at": now}
    notifier.notify(db_path)
    return {key: value for key, value in result.items() if key not in {"source_json", "report_json"}}
