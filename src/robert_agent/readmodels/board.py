"""Deterministic read models for the DD repository task board."""

from robert_agent.foundation.contracts.execution import BindingStatus
from robert_agent.foundation.contracts.work import (
    ACTIVE_TASK_LIFECYCLES,
    RemediationStatus,
    SourceProvider,
    TaskLifecycle,
    WorkCommand,
)

import base64
import json
from robert_agent.foundation.values import json_object
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from robert_agent.persistence import database as db_database
from robert_agent.persistence import execution_control as execution_store
from robert_agent.persistence import records as db_records
from robert_agent.persistence import transactions as db_transactions
from robert_agent.persistence.queries import attempts as db_read_attempts
from robert_agent.persistence.queries import github_sources as db_read_github_sources
from robert_agent.persistence.queries import repos as db_read_repos
from robert_agent.persistence.queries import (
    supervision_recovery_actions as db_read_supervision_recovery_actions,
)
from robert_agent.persistence.queries import wakeups as db_read_wakeups
from robert_agent.persistence.queries import work_items as db_read_work_items
from robert_agent.persistence.queries.readviews import board as board_queries
from robert_agent.persistence.queries.readviews import task_history
from robert_agent.persistence.queries.readviews import work_items as work_item_queries
from robert_agent.persistence.rounds import review as review_store
from robert_agent.persistence.rounds import pre_pr_review
from robert_agent.persistence.rounds import vote as vote_store
from robert_agent.readmodels.execution import (
    attempt_history,
    attempt_summary_from_records,
    execution_summary,
    publication_history,
    safe_text,
)
from robert_agent.work.messaging import selectable_choices
from robert_agent.work import control_queries
from robert_agent.work import inputs as work_items

COLUMNS = (
    ("backlog", "Backlog"),
    ("todo", "TODO"),
    ("doing", "Doing"),
    ("waiting", "Waiting for You"),
    ("review", "Review"),
    ("done", "Done"),
)
ATTENTION_EVENTS = frozenset(
    {
        "operator_question",
        "completion_acceptance_requested",
        "operator_decision_required",
        "execution_failed",
        "publication_failed",
        "manual_worker_unavailable",
        "review_attention",
        "unmerged_pr_closed",
    }
)
SAFE_EVENT_METADATA_KEYS = frozenset(
    {
        "kind",
        "task_id",
        "result_id",
        "source_id",
        "source_key",
        "workstream_id",
        "pr_number",
        "url",
        "route_confidence",
        "requested_worker",
        "supervision_decision_id",
    }
)


class BoardQueryError(ValueError):
    pass


def _connect(db_path):
    return db_database.connect(Path(db_path).expanduser(), foreign_keys=True, busy_timeout_ms=5000)


def _decode_json(value, default):
    if not value:
        return default
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, type(default)) else default
    except (TypeError, ValueError):
        return default


def _cursor_encode(values):
    raw = json.dumps(values, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _cursor_decode(value, *, board=False):
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BoardQueryError("invalid cursor") from exc
    if not isinstance(decoded, list):
        raise BoardQueryError("invalid cursor")
    if board and len(decoded) == 3:
        if type(decoded[0]) is not int or decoded[0] not in {0, 1}:
            raise BoardQueryError("invalid cursor")
        parts = decoded[1:]
    else:
        parts = decoded
    if len(parts) != 2 or not all(isinstance(part, str) and part for part in parts):
        raise BoardQueryError("invalid cursor")
    return tuple(decoded)


def _safe_event_metadata(value):
    metadata = _decode_json(value, {})
    if not isinstance(metadata, dict):
        return {}
    return {
        key: metadata[key]
        for key in SAFE_EVENT_METADATA_KEYS
        if key in metadata and isinstance(metadata[key], (str, int, float, bool, type(None)))
    }


def _safe_http_url(value):
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _unresolved_attention(conn, work_item_id):
    return _attention_signals(work_item_queries.unresolved_attention(conn, work_item_id, sorted(ATTENTION_EVENTS)))


def _attention_signals(rows):
    return [
        {
            "event_id": row["event_id"],
            "type": row["event_type"],
            "summary": safe_text(row["body"], 2000 if row["event_type"] == "operator_question" else 500),
            "choices": selectable_choices(json_object(row["metadata_json"]).get("choices", []))
            if row["event_type"] == "operator_question"
            else [],
            "created_at": row["created_at"],
            "metadata": _safe_event_metadata(row["metadata_json"]),
        }
        for row in rows
    ]


def _latest_attempt(conn, task_id):
    return db_records.first(db_read_attempts.find_for_latest_attempt(conn, task_id))


def _exhausted_pr_remediation(conn, pr, *, episodes=None):
    health = json_object(pr["metadata_json"]).get("pr_remediation_health")
    if not isinstance(health, dict):
        health = {}
    head, base = health.get("head_sha"), health.get("base_sha")
    latest = {}
    if episodes is None:
        episodes = task_history.remediation_for_workstreams(conn, [pr["workstream_id"]])
    for episode in episodes:
        if (
            head
            and base
            and (
                base != episode["observed_base_sha"]
                or head not in (episode["observed_head_sha"], episode["result_head_sha"])
            )
        ):
            continue
        latest.setdefault(episode["episode_kind"], episode)
    if health.get("mergeable") is True:
        latest.pop("merge_conflict", None)
    if health.get("ci_status") == "green":
        latest.pop("ci", None)
    return next((episode for episode in latest.values() if episode["status"] == RemediationStatus.EXHAUSTED), None)


def _valid_commands(column, attention, *, running=False, publishing=False):
    if column == "history":
        return []
    if column == "done":
        return ["reopen", "cancel"]
    if not publishing and any(a.get("metadata", {}).get("supervision_decision_id") for a in attention):
        return [WorkCommand.REPLY.value, WorkCommand.CANCEL.value]
    if publishing:
        return ["cancel"]
    if running:
        return (
            ["approve", "cancel"] if attention and attention[0]["type"] == "operator_decision_required" else ["cancel"]
        )
    if column == "todo":
        return ["cancel"]
    if column == "backlog":
        return ["start", "edit", "cancel"]
    if attention:
        types = {signal["type"] for signal in attention}
        commands = []
        if types & work_items.FAILURE_EVENTS:
            commands.append("retry")
        if types & work_items.QUESTION_EVENTS:
            commands.append("reply")
        if types & (work_items.DECISION_EVENTS | {"completion_acceptance_requested"}):
            commands.append("approve")
        if types & (work_items.QUESTION_EVENTS | work_items.FAILURE_EVENTS):
            commands.append("request_changes")
        return commands + ["cancel"]
    if column == "review":
        return ["request_changes", "cancel"]
    return ["cancel"]


def _waiting_source_context(conn, task_id):
    from robert_agent.work import operator_queries

    task = next(iter(operator_queries.pending_tasks(conn, task_id=task_id)), None)
    return bool(task and not operator_queries.source_read(conn, task)[0])


def _retrigger_pending(conn, task, *, loaded=None):
    if loaded is not None:
        return loaded["retrigger_pending"]
    return bool(db_records.first(db_read_wakeups.find_for_project_item(conn, task["task_id"])))


def _failed_task_recovery(conn, task, attention, *, loaded=None):
    """Commands that start a new run of a failed task, or none while one is owed."""
    binding = (
        loaded["recovery_bindings"].get(("task", task["task_id"]))
        if loaded is not None
        else execution_store.get_binding(conn, "task", task["task_id"])
    )
    if _retrigger_pending(conn, task, loaded=loaded) or (
        binding and binding["binding_status"] == BindingStatus.ATTENTION
    ):
        return []
    operator_failure = json_object(task["metadata_json"]).get("trigger_work_item_event_id") and any(
        a["type"] == "execution_failed" and a["metadata"].get("task_id") == task["task_id"] for a in attention
    )
    return ["retry" if operator_failure else "retrigger"]


# reason_code -> (state, tone, owner) for the waiting column.
WAITING_STATES = {
    "execution_failed": ("failed", "danger", "operator"),
    "workflow_attention": ("failed", "danger", "operator"),
    "pr_remediation_exhausted": ("failed", "danger", "operator"),
    "audit_inspection_exhausted": ("failed", "danger", "operator"),
    "review_attention": ("failed", "danger", "operator"),
    "publication_failed": ("publish_failed", "danger", "operator"),
    "manual_worker_unavailable": ("worker_unavailable", "danger", "operator"),
    "worker_cleanup_pending": ("environment", "danger", "admin"),
    "closed_source_running": ("source_closed", "attention", "operator"),
    "source_closed": ("source_closed", "attention", "operator"),
    "unmerged_pr_closed": ("pr_closed", "attention", "operator"),
    "operator_question": ("needs_reply", "attention", "initiator"),
    "completion_acceptance_requested": ("needs_acceptance", "attention", "initiator"),
    "operator_decision_required": ("needs_decision", "attention", "operator"),
    "no_runnable_task": ("stuck", "attention", "operator"),
}
REVIEW_STATES = {
    "open_pr": ("in_review", "info", "github"),
    "unmerged_pr_closed": ("pr_closed", "attention", "operator"),
}
PRIMARY_COMMANDS = ("retrigger", "retry", "retry_publish", "reply", "approve", "start", "reopen")


def _task_attempt_counts(conn, task_id):
    total, failed = db_read_attempts.count_by_task_status(conn, task_id)[0]
    return {"total": total, "failed": failed}


def _summary_cause(card, facts, attention, state):
    """The single most specific operator-facing reason, with where it came from."""
    execution = card.get("execution") or {}
    review = card.get("vote") or card.get("review") or {}
    candidates = []
    if card["reason_code"] == "pr_remediation_exhausted":
        candidates.append(("remediation", card["reason_summary"], None))
    if state in {"failed", "worker_unavailable"}:
        candidates += [
            ("review", review.get("error"), execution.get("finished_at")),
            ("attempt", execution.get("failure"), execution.get("finished_at")),
        ]
    if state == "publish_failed":
        failure = (card["publication"]["failures"] or [{}])[0]
        candidates.append(("publication", failure.get("reason"), failure.get("failed_at")))
    if state == "environment":
        candidates.append(("cleanup", (facts["cleanup_pending"] or [""])[0], None))
    if state == "failed" and facts["workflow_attention"]:
        candidates.append(("workflow", facts["workflow_attention"][0], None))
    if attention:
        candidates.append(("attention", attention[0]["summary"], attention[0]["created_at"]))
    for source, text, at in candidates:
        text = (text or "").strip()
        if text:
            return {
                "text": text,
                "source": source,
                "at": at,
                "attempt_no": execution.get("attempt_no") if source in {"attempt", "review"} else None,
            }
    return None


def _status_summary(conn, card, row, *, task, facts, attention, loaded=None):
    """Operator-facing status shared by cards, details and attention counts.

    Values are codes; the web client owns their wording in each language.
    """
    column, reason = card["column"], card["reason_code"]
    if column == "history":
        state, tone, owner = "canceled", "neutral", "none"
    elif column == "done":
        state, tone, owner = "completed", "success", "none"
    elif column == "backlog":
        state, tone, owner = "draft", "neutral", "operator"
    elif column == "todo":
        state, tone, owner = "queued", "neutral", "robert"
    elif column == "doing":
        state, tone, owner = ("publishing" if reason == "publishing" else "running"), "running", "robert"
    elif column == "review":
        state, tone, owner = REVIEW_STATES.get(reason, ("review_attention", "attention", "operator"))
    else:
        state, tone, owner = WAITING_STATES.get(reason, ("needs_attention", "attention", "operator"))
    commands = card["valid_commands"]
    primary = next((command for command in PRIMARY_COMMANDS if command in commands), None)
    if state == "in_review" and card.get("pr") and card["pr"].get("url"):
        primary = "open_pr"
    elif reason == "pr_remediation_exhausted" and "request_changes" in commands:
        primary = "request_changes"
    elif state in {"completed", "canceled", "queued", "running", "publishing"}:
        primary = None
    execution = card.get("execution") or {}
    return {
        "state": state,
        "tone": tone,
        "owner": owner,
        "owner_identity": row["created_by"] if owner == "initiator" else None,
        "needs_action": owner in {"operator", "initiator", "admin"} and state != "draft",
        "stage": {
            "task": (task["expected_output"] or task["route_id"]) if task else None,
            "phase": execution.get("phase"),
        },
        "attempts": loaded["attempt_counts"]
        if loaded is not None
        else (_task_attempt_counts(conn, task["task_id"]) if task else {"total": 0, "failed": 0}),
        "cause": _summary_cause(card, facts, attention, state),
        "primary": primary,
        "since": execution.get("finished_at") or execution.get("heartbeat_at") or row["updated_at"],
    }


def _project_item(conn, row, *, controls=None, facts=None, loaded=None):
    work_item_id = row["work_item_id"]
    if loaded is None:
        attention = _unresolved_attention(conn, work_item_id)
        source = db_records.first(
            db_read_github_sources.find_source_type_and_number_and_html_url_by_source_id(conn, row["origin_source_id"])
        )
        if facts is None:
            facts = control_queries.lifecycle_facts(conn, row, controls=controls)
        task = control_queries.current_task(conn, facts["streams"])
        attempt = _latest_attempt(conn, task["task_id"]) if task else None
        execution = execution_summary(conn, task["task_id"]) if task else None
    else:
        attention, source, task, attempt, execution = (
            loaded[key] for key in ("attention", "source", "task", "attempt", "execution")
        )
        facts = loaded["facts"]
    pr = next(iter(facts["pull_requests"]), None)
    exhausted_remediation = None
    if pr and (pr["state"] or "open").lower() == "open":
        exhausted_remediation = _exhausted_pr_remediation(
            conn, pr, episodes=loaded["remediation"] if loaded is not None else None
        )
    running = bool(facts["running"])
    publication_in_progress = bool(facts["actions"])
    failures = facts["publish_failures"]
    unavailable = next(
        (
            a
            for a in attention
            if a["type"] == "manual_worker_unavailable" and task and a["metadata"].get("task_id") == task["task_id"]
        ),
        None,
    )

    # A completed item whose latest task failed afterwards still needs the
    # operator; only a closed source ends the item regardless of that failure.
    failed_after_completion = bool(
        row["completed_at"]
        and not row["canceled_at"]
        and task
        and task["lifecycle"] == TaskLifecycle.FAILED
        and not running
        and not publication_in_progress
        and not facts["source_closed"]
    )
    rerun_requested = bool(
        failed_after_completion and row["origin_type"] != "web" and _retrigger_pending(conn, task, loaded=loaded)
    )
    if row["canceled_at"]:
        column, reason_code, reason_summary = "history", "canceled", "Canceled by the operator"
    elif rerun_requested:
        column, reason_code, reason_summary = (
            "todo",
            "rerun_requested",
            "A new run was requested; waiting for the daemon to start it.",
        )
    elif failed_after_completion:
        column, reason_code, reason_summary = (
            "waiting",
            "execution_failed",
            (execution or {}).get("failure")
            or "The latest run failed after this item completed; inspect it and run it again.",
        )
    elif row["completed_at"]:
        column, reason_code, reason_summary = "done", "completed", "Completion is satisfied"
    elif facts["source_closed"] and running:
        column, reason_code, reason_summary = (
            "waiting",
            "closed_source_running",
            "Source closed; the worker still needs to be stopped.",
        )
    elif facts["workflow_attention"]:
        column, reason_code, reason_summary = (
            "waiting",
            "workflow_attention",
            "Automatic execution stopped: " + facts["workflow_attention"][0],
        )
    elif facts["cleanup_pending"]:
        column, reason_code, reason_summary = (
            "waiting",
            "worker_cleanup_pending",
            "Worker cleanup is incomplete: " + facts["cleanup_pending"][0],
        )
    elif task and json_object(task["metadata_json"]).get("audit_retry_required") and not running:
        column, reason_code, reason_summary = (
            "waiting",
            "audit_inspection_exhausted",
            "Audit inspection stopped. Retry the saved result after repairing Git access.",
        )
    elif failures and not running:
        column, reason_code, reason_summary = (
            "waiting",
            "publication_failed",
            failures[0]["reason"] or "Publication failed; retry the existing result.",
        )
    elif unavailable and not running and not publication_in_progress:
        column, reason_code, reason_summary = "waiting", "manual_worker_unavailable", unavailable["summary"]
    elif (
        attention
        and all(a["type"] == "operator_decision_required" for a in attention)
        and not facts["approval_task_id"]
        and not running
        and not publication_in_progress
    ):
        column, reason_code, reason_summary = (
            "waiting",
            "no_runnable_task",
            "No active task remains for this decision. Refresh the source state or cancel this item.",
        )
    elif (
        task
        and task["lifecycle"] == TaskLifecycle.DETECTED
        and row["origin_type"] in set(SourceProvider)
        and not running
        and not publication_in_progress
        and not any(a["type"] == "operator_decision_required" for a in attention)
        and _waiting_source_context(conn, task["task_id"])
    ):
        source_name = "TAPD" if row["origin_type"] == SourceProvider.TAPD else "GitHub"
        column, reason_code, reason_summary = (
            "todo",
            f"{row['origin_type']}_context_refresh_pending",
            f"Queued; waiting for the background scanner to refresh {source_name} context.",
        )
    elif (
        task
        and task["lifecycle"]
        in {TaskLifecycle.DETECTED, TaskLifecycle.AUTHORIZED, TaskLifecycle.CLASSIFIED, TaskLifecycle.QUEUED}
        and not running
        and not publication_in_progress
        and not any(a["type"] == "operator_decision_required" for a in attention)
    ):
        column, reason_code, reason_summary = "todo", task["lifecycle"], "Runnable work is queued"
    elif (
        attention
        and (not running or attention[0]["type"] == "operator_decision_required")
        and not publication_in_progress
        and not (pr and attention[0]["type"] in {"publication_failed", "unmerged_pr_closed", "review_attention"})
    ):
        column, reason_code, reason_summary = (
            "waiting",
            attention[0]["type"],
            attention[0]["summary"] or "Operator input is required",
        )
    elif running or publication_in_progress:
        column, reason_code, reason_summary = (
            "doing",
            ("publishing" if publication_in_progress and not running else "running"),
            "Agent execution or publication is in progress",
        )
    elif pr and attention and attention[0]["type"] in {"publication_failed", "unmerged_pr_closed", "review_attention"}:
        column, reason_code, reason_summary = (
            "review",
            attention[0]["type"],
            attention[0]["summary"] or "Pull request review needs attention",
        )
    elif task and task["lifecycle"] == TaskLifecycle.FAILED:
        column, reason_code, reason_summary = (
            "waiting",
            "execution_failed",
            (execution or {}).get("failure") or "Execution failed; inspect the attempt and retry.",
        )
    elif facts["source_closed"] and not facts["open_pull_requests"]:
        column, reason_code, reason_summary = (
            "waiting",
            "source_closed",
            "Source closed; inspect outstanding delivery or cleanup before finishing this item.",
        )
    elif task and task["lifecycle"] in ACTIVE_TASK_LIFECYCLES:
        column, reason_code, reason_summary = "todo", task["lifecycle"], "Runnable work is queued"
    elif exhausted_remediation:
        reason = safe_text(json_object(exhausted_remediation["metadata_json"]).get("last_transition_reason"))
        column, reason_code, reason_summary = (
            "waiting",
            "pr_remediation_exhausted",
            f"Automated PR remediation stopped: {reason or 'budget_exhausted'}.",
        )
    elif pr and (pr["state"] or "open").lower() == "open":
        column, reason_code, reason_summary = "review", "open_pr", f"PR #{pr['number']} is open"
    elif not row["activated_at"]:
        column, reason_code, reason_summary = "backlog", "draft", "Requirement is editable"
    else:
        column, reason_code, reason_summary = (
            "waiting",
            "no_runnable_task",
            "No runnable task is available. Inspect the source state or cancel this item.",
        )

    routing_mode = task["routing_mode"] if task else row["routing_mode"]
    requested_worker = task["requested_worker"] if task else row["requested_worker"]
    card = {
        "work_item_id": work_item_id,
        "repo_id": row["repo_id"],
        "repo_full_name": row["repo_full_name"],
        "title": row["title"],
        "priority": row["priority"],
        "origin_type": row["origin_type"],
        "column": column,
        "reason_code": reason_code,
        "reason_summary": reason_summary,
        "routing_mode": routing_mode,
        "agent": requested_worker if routing_mode == "manual" else "Auto",
        "task_id": task["task_id"] if task else None,
        "task_lifecycle": task["lifecycle"] if task else None,
        "execution": execution,
        "review": loaded["rounds"]["review"]
        if loaded is not None
        else (review_store.board_status(conn, task["task_id"]) if task else None),
        "pre_pr_review": loaded["rounds"]["pre_pr_review"]
        if loaded is not None
        else (pre_pr_review.task_status(conn, task["task_id"]) if task else None),
        "vote": loaded["rounds"]["vote"]
        if loaded is not None
        else (vote_store.board_status(conn, task["task_id"]) if task else None),
        "publication": {
            "failures": failures,
            "pending": len(facts["actions"]),
            "retry_pending": facts["publication_retry_pending"],
            "blocked_reason": "Publication reconciliation is blocked by the audit or ownership boundary. Cancel this task and resolve the reported cause before creating new work."
            if facts["publication_retry_blocked"]
            else None,
        },
        "action_blocked_reason": "publication_in_progress" if publication_in_progress and not failures else None,
        "branch": attempt["branch_name"] if attempt else None,
        "worktree_label": Path(attempt["worktree_path"]).name if attempt and attempt["worktree_path"] else None,
        "source": (
            {"type": source["source_type"], "number": source["number"], "url": _safe_http_url(source["html_url"])}
            if source
            else None
        ),
        "pr": (
            {
                "number": pr["number"],
                "url": _safe_http_url(pr["html_url"]),
                "state": pr["state"],
            }
            if pr
            else None
        ),
        "attention_signals": attention,
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    card["valid_commands"] = _valid_commands(column, attention, running=running, publishing=publication_in_progress)
    if reason_code == "pr_remediation_exhausted":
        card["valid_commands"] = ["request_changes", "cancel"]
    if not row["canceled_at"] and not row["completed_at"]:
        if facts["source_closed"] or facts["cleanup_pending"]:
            card["valid_commands"] = ["cancel"]
        elif (
            task
            and json_object(task["metadata_json"]).get("execution_attention", {}).get("reason_code")
            == "result_contract_cutover"
        ):
            from robert_agent.execution import rounds as native_rounds

            _, owner = native_rounds.owner(conn, task["task_id"])
            prepared = db_records.first(db_read_attempts.find_for_project_item(conn, task["task_id"]))
            pending = db_records.first(db_read_wakeups.find_for_project_item(conn, task["task_id"]))
            native_stopped = not owner or db_records.first(
                db_read_attempts.find_for_project_item_2(conn, task["task_id"])
            )
            recoverable = (
                row["origin_type"] != "web"
                and native_stopped
                and not (running or publication_in_progress or prepared or pending)
            )
            card["valid_commands"] = (["retrigger"] if recoverable else []) + ["cancel"]
        elif task and json_object(task["metadata_json"]).get("audit_retry_required") and not running:
            card["valid_commands"] = ["retry", "cancel"]
        elif unavailable and not running and not publication_in_progress:
            card["valid_commands"] = ["retry", "cancel"]
        elif failures and not running:
            card["valid_commands"] = (
                ["retry_publish"]
                if not facts["publication_retry_pending"] and not facts["publication_retry_blocked"]
                else []
            ) + ["cancel"]
        elif task and task["lifecycle"] == TaskLifecycle.FAILED and not running and not publication_in_progress:
            if row["origin_type"] != "web":
                card["valid_commands"] = _failed_task_recovery(conn, task, attention, loaded=loaded) + ["cancel"]
            else:
                card["valid_commands"] = ["retry", "cancel"]
    if not facts["approval_task_id"] and any(a["type"] == "operator_decision_required" for a in attention):
        if not any(a["type"] == "completion_acceptance_requested" for a in attention):
            card["valid_commands"] = [command for command in card["valid_commands"] if command != "approve"]
    if rerun_requested:
        # Reopening now would start a second run beside the requested one.
        card["valid_commands"] = ["cancel"]
    elif failed_after_completion:
        # A rerun acts on the failed task; reopening and cancellation act on
        # the work item, retaining its prior completion evidence.
        recovery = _failed_task_recovery(conn, task, attention, loaded=loaded) if row["origin_type"] != "web" else []
        card["valid_commands"] = [command for command in recovery if command == "retrigger"] + ["reopen", "cancel"]
    if column == "done" and facts["source_closed"]:
        card["valid_commands"] = ["cancel"]
    card["status_summary"] = _status_summary(
        conn, card, row, task=task, facts=facts, attention=attention, loaded=loaded
    )
    return card


def _batch_projections(conn, rows, *, controls=None, workstream=False):
    from collections import defaultdict

    records = board_queries.lifecycle_records(conn, rows, controls=controls)
    attention = defaultdict(list)
    for event in work_item_queries.unresolved_attention_for_items(conn, records, sorted(ATTENTION_EVENTS)):
        attention[event["work_item_id"]].append(event)
    task_ids = [record["task"]["task_id"] for record in records.values() if record["task"]]
    # Lifecycle callers may supply partial or overridden observations. Recovery
    # commands retain the independent persisted-binding check of item detail.
    recovery_bindings = execution_store.read_controls(conn, [("task", task_id) for task_id in task_ids])
    execution = board_queries.current_execution_records(conn, task_ids)
    rounds = board_queries.model_round_statuses(conn, task_ids)
    remediation = defaultdict(list)
    pull_requests = [
        pr
        for record in records.values()
        for pr in record["pull_requests"][:1]
        if (pr["state"] or "open").lower() == "open"
    ]
    for episode in board_queries.latest_remediation_records(conn, pull_requests):
        remediation[episode["workstream_id"]].append(episode)
    cards = []
    for row in rows:
        record = records[row["work_item_id"]]
        relevant_controls = record["controls"]
        if controls is not None:
            relevant_controls = {key: controls[key] for key in relevant_controls if key in controls}
        source = record["source"]
        facts = control_queries.lifecycle_facts_from_records(
            streams=record["streams"],
            attempts=record["attempts"],
            actions=record["actions"],
            task_metadata=record["task_metadata"],
            subjects=relevant_controls,
            controls=relevant_controls,
            source_state=source["state"] if source else None,
            publication_retry_pending=record["publication_retry_pending"],
            approval_task_id=record["approval_task_id"],
            pull_requests=record["pull_requests"],
        )
        task = record["task"]
        task_id = task["task_id"] if task else None
        attempt = execution["attempts"].get(task_id)
        loaded = {
            "facts": facts,
            "source": source,
            "task": task,
            "attempt": attempt,
            "execution": attempt_summary_from_records(attempt, execution["phases"].get(attempt["attempt_id"]))
            if attempt
            else None,
            "attention": _attention_signals(attention[row["work_item_id"]]),
            "controls": relevant_controls,
            "recovery_bindings": recovery_bindings,
            "retrigger_pending": task_id in execution["retriggers"],
            "attempt_counts": execution["counts"].get(task_id, {"total": 0, "failed": 0}),
            "rounds": rounds.get(task_id, {"review": None, "vote": None, "pre_pr_review": None}),
            "remediation": remediation[record["pull_requests"][0]["workstream_id"]] if record["pull_requests"] else [],
        }
        card = _project_item(conn, row, loaded=loaded)
        if workstream:
            card["operator_cancellation_settled"] = bool(row["canceled_at"]) and not any(
                facts[key] for key in ("running", "actions", "workflow_attention", "cleanup_pending")
            )
        cards.append(card)
    return cards


def _all_projections(conn, *, repo=None, include_history=True, active_only=False, controls=None):
    rows = work_item_queries.work_items_with_repositories(
        conn, repo=repo, include_history=include_history, active_only=active_only
    )
    return _batch_projections(conn, rows, controls=controls)


def workstream_projections(conn, *, workstream_ids=None, controls=None):
    """Share board lifecycle facts with the workbench without changing its buckets."""
    rows = work_item_queries.work_items_with_repositories(
        conn,
        workstream_ids=workstream_ids,
        attached_only=True,
    )
    cards = _batch_projections(conn, rows, controls=controls, workstream=True)
    return {row["workstream_id"]: card for row, card in zip(rows, cards)}


def _matches(item, *, repo, agent, priority, attention, query):
    if repo and repo not in {item["repo_id"], item["repo_full_name"]}:
        return False
    if agent and item["agent"] != agent:
        return False
    if priority and item["priority"] != priority:
        return False
    if attention is not None:
        wanted = attention in {True, "true", "1", "yes"}
        if (item["status_summary"]["needs_action"] or bool(item["attention_signals"])) != wanted:
            return False
    if query:
        haystack = " ".join(
            str(value or "")
            for value in (
                item["title"],
                item["work_item_id"],
                item["task_id"],
                item["branch"],
                (item["pr"] or {}).get("number"),
            )
        ).lower()
        if query.lower() not in haystack:
            return False
    return True


def list_board(
    db_path,
    *,
    repo=None,
    column=None,
    agent=None,
    priority=None,
    attention=None,
    query="",
    limit=50,
    cursor=None,
    include_history=False,
):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise BoardQueryError("limit must be between 1 and 200")
    valid_columns = {column_id for column_id, _label in COLUMNS} | {"history"}
    if column and column not in valid_columns:
        raise BoardQueryError("invalid column")
    if priority and priority not in {"P0", "P1", "P2", "P3"}:
        raise BoardQueryError("invalid priority")
    if attention is not None and attention not in {True, False, "true", "false", "1", "0", "yes", "no"}:
        raise BoardQueryError("attention must be a boolean")
    cursor_value = _cursor_decode(cursor, board=True)
    legacy_cursor = bool(cursor_value and len(cursor_value) == 2)

    def page_key(item):
        updated = (item["updated_at"], item["work_item_id"])
        return updated if legacy_cursor else (int(item["column"] not in {"done", "history"}), *updated)

    with closing(_connect(db_path)) as conn:
        db_transactions.begin(conn)  # Historical and active partitions share one read snapshot.
        terminal = work_item_queries.terminal_board_page(
            conn,
            repo=repo,
            column=column,
            agent=agent,
            priority=priority,
            attention=attention,
            query=query,
            before=cursor_value[-2:] if cursor_value and (legacy_cursor or cursor_value[0] == 0) else None,
            limit=limit + 1,
            include_history=include_history,
            attention_types=sorted(ATTENTION_EVENTS),
        )
        projections = [
            item
            for item in _all_projections(conn, repo=repo, include_history=include_history, active_only=True)
            if _matches(item, repo=repo, agent=agent, priority=priority, attention=attention, query=query)
        ]
        counts = {
            column_id: sum(1 for item in projections if item["column"] == column_id)
            + terminal["counts"].get(column_id, 0)
            for column_id in [name for name, _label in COLUMNS] + ["history"]
        }
        attention_items = [item for item in projections if item["status_summary"]["needs_action"]]
        counts["needs_action"] = len(attention_items)
        attention_preview = [
            {key: item[key] for key in ("work_item_id", "title", "repo_full_name", "column", "status_summary")}
            for item in attention_items[:8]
        ]
        agents = sorted({item["agent"] for item in projections} | set(terminal["agents"]) | {"Auto"})
        if cursor_value:
            projections = [item for item in projections if page_key(item) < cursor_value]
        if column:
            projections = [item for item in projections if item["column"] == column]
        page = sorted(
            projections + [dict(row) for row in terminal["rows"]],
            key=page_key,
            reverse=True,
        )[: limit + 1]
        has_more = len(page) > limit
        selected = page[:limit]
        terminal_ids = {row["work_item_id"] for row in terminal["rows"]}
        selected_ids = [item["work_item_id"] for item in selected if item["work_item_id"] in terminal_ids]
        historical = (
            {
                card["work_item_id"]: card
                for card in _batch_projections(
                    conn, work_item_queries.work_items_with_repositories(conn, item_ids=selected_ids)
                )
            }
            if selected_ids
            else {}
        )
        items = [historical.get(item["work_item_id"], item) for item in selected]
        next_cursor = _cursor_encode(page_key(items[-1])) if has_more and items else None
        running_rows = db_read_attempts.list_board(conn)
        per_repo_running = {row[0]: row[1] for row in running_rows}
        repos = [
            {"repo_id": row[0], "full_name": row[1], "running": per_repo_running.get(row[0], 0)}
            for row in db_read_repos.list_board(conn)
        ]
    return {
        "columns": [{"id": column_id, "label": label, "count": counts[column_id]} for column_id, label in COLUMNS],
        "counts": counts,
        "attention_items": attention_preview,
        "capacity": {
            "running": sum(per_repo_running.values()),
            "by_repo": per_repo_running,
        },
        "filters": {
            "repos": repos,
            "agents": agents,
            "priorities": ["P0", "P1", "P2", "P3"],
        },
        "items": items,
        "next_cursor": next_cursor,
    }


def _supervision_decision(conn, workstream_id):
    from robert_agent.persistence.queries import supervision_decisions as decision_queries
    from robert_agent.supervision import decisions

    root = decision_queries.root_stream(conn, workstream_id) if workstream_id else None
    if not root:
        return None
    state = decisions.read(conn, root["workstream_id"])
    return (
        {
            "workstream_id": root["workstream_id"],
            **{
                key: state[key]
                for key in ("status", "decision", "next_check_at", "receipt", "notification_id")
                if key in state
            },
            **(
                {
                    "last_failure": {
                        key: safe_text(value, 2000) if isinstance(value, str) else value
                        for key, value in state["last_failure"].items()
                        if key in {"decision_id", "status", "failed_at", "safe_error", "exit_code", "stderr_truncated"}
                    }
                }
                if isinstance(state.get("last_failure"), dict)
                else {}
            ),
        }
        if state
        else None
    )


def _supervision_incidents(conn, tasks, work_item_id):
    from robert_agent.supervision import store as supervision_store

    if not supervision_store.has_schema(conn):
        return []
    task_ids = [task["task_id"] for task in tasks]
    rows = work_item_queries.supervision_incidents(conn, work_item_id, task_ids)
    result = []
    for row in rows:
        item = dict(row)
        analysis = supervision_store.get_checkpoint(conn, "diagnosis", row["incident_id"], "analysis")
        diagnosis = (analysis or {}).get("metadata", {}).get("diagnosis") or {}
        item["diagnosis"] = {
            key: safe_text(diagnosis.get(key), 1000) for key in ("diagnosis_class", "expected_progress")
        }
        repair = supervision_store.get_checkpoint(conn, "repair", row["incident_id"], "engineering")
        verification = supervision_store.get_checkpoint(conn, "repair", row["incident_id"], "verification")
        item["repair_work_item_id"] = (repair or {}).get("metadata", {}).get("work_item_id")
        item["repair_verification"] = {
            key: value.get("status")
            for key, value in (verification or {}).get("metadata", {}).items()
            if isinstance(value, dict)
        }
        item["recovery_actions"] = [
            dict(action)
            for action in db_read_supervision_recovery_actions.find_action_kind_and_status_and_budget_cost_by_incident_id(
                conn, row["incident_id"]
            )
        ]
        result.append(item)
    return result


def get_work_item_detail(db_path, work_item_id):
    with closing(_connect(db_path)) as conn:
        row = db_records.first(db_read_work_items.find_for_get_work_item_detail(conn, work_item_id))
        if not row:
            return None
        card = _project_item(conn, row)
        tasks = []
        if row["workstream_id"]:
            streams = control_queries.related_workstreams(conn, row["workstream_id"])
            task_rows = task_history.tasks_for_workstreams(conn, streams, chronological=True)
            for task in task_rows:
                attempts = [
                    {
                        **attempt_history(conn, attempt),
                        "status": attempt["status"],
                        "branch": attempt["branch_name"],
                        "worktree_label": Path(attempt["worktree_path"]).name if attempt["worktree_path"] else None,
                        "started_at": attempt["started_at"],
                        "finished_at": attempt["finished_at"],
                    }
                    for attempt in db_read_attempts.find_record_by_task_id(conn, task["task_id"])
                ]
                tasks.append(
                    {
                        "task_id": task["task_id"],
                        "parent_task_id": task["parent_task_id"],
                        "lifecycle": task["lifecycle"],
                        "priority": task["priority"],
                        "route_id": task["route_id"],
                        "expected_output": task["expected_output"],
                        "routing_mode": task["routing_mode"],
                        "requested_worker": task["requested_worker"],
                        "created_at": task["created_at"],
                        "updated_at": task["updated_at"],
                        "attempts": attempts,
                        "review": review_store.board_status(conn, task["task_id"]),
                        "pre_pr_review": pre_pr_review.task_status(conn, task["task_id"]),
                        "vote": vote_store.board_status(conn, task["task_id"]),
                        "publication_actions": publication_history(conn, task["task_id"]),
                    }
                )
        timeline = list_work_item_events(db_path, work_item_id, limit=20)
        return {
            **card,
            "description": row["description"],
            "created_by": row["created_by"],
            "activated_at": row["activated_at"],
            "completed_at": row["completed_at"],
            "canceled_at": row["canceled_at"],
            "tasks": tasks,
            "supervision_incidents": _supervision_incidents(conn, tasks, work_item_id),
            "supervision_decision": _supervision_decision(conn, row["workstream_id"]),
            "events": timeline["events"],
            "events_next_cursor": timeline["next_cursor"],
        }


def list_work_item_events(db_path, work_item_id, *, limit=50, cursor=None):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise BoardQueryError("limit must be between 1 and 200")
    cursor_value = _cursor_decode(cursor)
    with closing(_connect(db_path)) as conn:
        exists = db_records.first(db_read_work_items.list_work_item_events(conn, work_item_id))
        if not exists:
            return {"events": [], "next_cursor": None}
        rows = work_item_queries.event_page(conn, work_item_id, before=cursor_value, limit=limit + 1)
        has_more = len(rows) > limit
        rows = rows[:limit]
        events = [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "actor_kind": row["actor_kind"],
                "actor_identity": row["actor_identity"],
                "body": safe_text(row["body"], 8000),
                "resolves_event_id": row["resolves_event_id"],
                "created_at": row["created_at"],
                "metadata": _safe_event_metadata(row["metadata_json"]),
            }
            for row in rows
        ]
        next_cursor = _cursor_encode([rows[-1]["created_at"], rows[-1]["event_id"]]) if has_more and rows else None
    return {"events": events, "next_cursor": next_cursor}
