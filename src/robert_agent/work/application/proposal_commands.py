"""Versioned Web decisions that durably enqueue an approved proposal."""

from contextlib import closing
import json
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from robert_agent.adapters.notifications import wakeup as notifier
from robert_agent.foundation import values
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import proposals as queries
from robert_agent.persistence.stores import proposals as stores
from robert_agent.security.text import safe_text
from robert_agent.work import wakeup
from robert_agent.work.errors import WorkItemConflictError, WorkItemNotFoundError, WorkItemValidationError
from robert_agent.work.inputs import validate_idempotency_key, validate_text


_ACTIONS = {
    "approve": "approved",
    "investigate": "approved",
    "hold": "held",
    "discard": "discarded",
    "restore": "pending",
    "assign": "blocked",
}


def _defer_until(now, days):
    zone = ZoneInfo("Asia/Shanghai")
    review_date = utc_timestamp(now).astimezone(zone).date() + timedelta(days=days)
    return datetime.combine(review_date, time(), tzinfo=zone).astimezone(timezone.utc).isoformat()


def decide(
    db_path,
    proposal_id,
    *,
    context,
    action,
    expected_version,
    idempotency_key,
    reason="",
    defer_days=None,
    repository_label=None,
):
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
    if defer_days is not None and (action != "hold" or type(defer_days) is not int or not 1 <= defer_days <= 365):
        raise WorkItemValidationError("deferral requires a whole number of days between 1 and 365")
    if repository_label is not None and (
        action not in {"approve", "investigate", "assign"} or not isinstance(repository_label, str)
    ):
        raise WorkItemValidationError("invalid proposal repository choice")
    options = {"defer_days": defer_days, "repository_label": repository_label}
    now = values.utc_now()
    with closing(database.connect(db_path)) as conn, conn:
        transactions.begin_immediate(conn)
        rows = queries.current(conn, proposal_id=proposal_id, repo_ids=context.allowed_repo_ids)
        if not rows:
            raise WorkItemNotFoundError("proposal not found")
        row = rows[0]
        previous = queries.get_decision(conn, proposal_id, idempotency_key=idempotency_key)
        if previous:
            if (
                previous["actor"] != context.actor_identity
                or previous["action"] != action
                or previous["version"] != expected_version + 1
                or previous["reason"] != reason
                or values.json_object(previous["options_json"])
                != {key: value for key, value in options.items() if value is not None}
            ):
                raise WorkItemConflictError("idempotency key was used for a different proposal decision")
            return {key: value for key, value in row.items() if key not in {"source_json", "report_json"}}
        if row["version"] != expected_version:
            raise WorkItemConflictError("proposal version changed; refresh before deciding")
        state = row["status"]
        if action == "restore":
            if state not in {"discarded", "held"}:
                raise WorkItemConflictError("only held or discarded proposals can be restored")
        elif state not in {"pending", "held", "blocked"}:
            raise WorkItemConflictError("proposal has already been decided")
        if state == "blocked" and action in {"approve", "investigate"}:
            raise WorkItemConflictError("proposal needs refreshed evidence before approval")
        source = values.json_object(row["source_json"])
        report = values.json_object(row["report_json"])
        repo_id = row["repo_id"]
        selected = None
        if repository_label is not None:
            selected = next(
                (rule for rule in source.get("repository_candidates", []) if rule["label"] == repository_label), None
            )
            if selected is None:
                raise WorkItemValidationError("repository choice is not available for this proposal")
            repo_id = values.repository_id(selected["repository"])
            if repo_id not in context.allowed_repo_ids:
                raise WorkItemNotFoundError("proposal repository not found")
        if action == "assign" and not selected:
            raise WorkItemValidationError("select a repository before refreshing the proposal")
        if action in {"approve", "investigate"}:
            if not repo_id or (source.get("repository_candidates") and repository_label is None):
                raise WorkItemValidationError("select a repository before starting the proposal")
            if report.get("feasibility") in {"unassessed", "needs_clarification"} or (
                selected and report.get("repository") != selected["repository"]
            ):
                raise WorkItemConflictError("proposal needs code evidence for the selected repository")
            if action == "approve" and report.get("feasibility") == "investigate":
                raise WorkItemConflictError("this proposal currently supports investigation")
        defer_until = _defer_until(now, defer_days) if defer_days else None
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
                "options_json": json.dumps({key: value for key, value in options.items() if value is not None}),
                "created_at": now,
            },
        )
        stores.revise(
            conn,
            proposal_id,
            row["version"],
            version=version,
            status=_ACTIONS[action],
            blocked_reason="等待对所选仓库重新分析。" if action == "assign" else "",
            updated_at=now,
            repo_id=repo_id,
            defer_until=defer_until,
        )
        if action in {"approve", "investigate"}:
            wakeup.request_wakeup(
                conn,
                repo_id=repo_id,
                reason="manual_operator_request",
                dedupe_key=f"proposal:{proposal_id}:{version}",
                metadata={"kind": "proposal_approved"},
                now=now,
            )
        result = {
            **row,
            "version": version,
            "status": _ACTIONS[action],
            "updated_at": now,
            "repo_id": repo_id,
            "defer_until": defer_until,
        }
    notifier.notify(db_path)
    return {key: value for key, value in result.items() if key not in {"source_json", "report_json"}}
