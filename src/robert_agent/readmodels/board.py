"""Deterministic read models for the DD repository task board."""

from robert_agent.foundation.contracts.execution import BindingStatus
from robert_agent.foundation.contracts.work import ACTIVE_TASK_LIFECYCLES, TaskLifecycle

import base64
import json
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from robert_agent.persistence import database as db_database
from robert_agent.persistence import execution_control as execution_store
from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import attempts as db_read_attempts
from robert_agent.persistence.queries import github_sources as db_read_github_sources
from robert_agent.persistence.queries import repos as db_read_repos
from robert_agent.persistence.queries import (
    supervision_recovery_actions as db_read_supervision_recovery_actions,
)
from robert_agent.persistence.queries import wakeups as db_read_wakeups
from robert_agent.persistence.queries import work_items as db_read_work_items
from robert_agent.persistence.queries import workstreams as db_read_workstreams
from robert_agent.persistence.queries.readviews import task_history
from robert_agent.persistence.queries.readviews import work_items as work_item_queries
from robert_agent.persistence.rounds import review as review_store
from robert_agent.persistence.rounds import vote as vote_store
from robert_agent.readmodels.execution import (
    attempt_history,
    execution_summary,
    publication_history,
    safe_text,
)
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
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _cursor_encode(values):
    raw = json.dumps(values, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _cursor_decode(value):
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BoardQueryError("invalid cursor") from exc
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or not all(isinstance(part, str) and part for part in decoded)
    ):
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
    rows = work_item_queries.unresolved_attention(conn, work_item_id, sorted(ATTENTION_EVENTS))
    return [
        {
            "event_id": row["event_id"],
            "type": row["event_type"],
            "summary": safe_text(row["body"], 500),
            "created_at": row["created_at"],
            "metadata": _safe_event_metadata(row["metadata_json"]),
        }
        for row in rows
    ]


def _latest_attempt(conn, task_id):
    return db_records.first(db_read_attempts.find_for_latest_attempt(conn, task_id))


def _derived_pr(conn, root_workstream_id):
    return db_records.first(db_read_workstreams.find_for_derived_pr(conn, root_workstream_id, root_workstream_id))


def _valid_commands(column, attention, *, running=False, publishing=False):
    if column == "history":
        return []
    if column == "done":
        return ["reopen"]
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


def _waiting_github_context(conn, task_id):
    from robert_agent.work import operator_queries as github_operator

    task = next(iter(github_operator.pending_tasks(conn, task_id=task_id)), None)
    return bool(task and not github_operator.source_read(conn, task)[0])


def _project_item(conn, row, *, controls=None, facts=None):
    work_item_id = row["work_item_id"]
    workstream_id = row["workstream_id"]
    attention = _unresolved_attention(conn, work_item_id)
    source = db_records.first(
        db_read_github_sources.find_source_type_and_number_and_html_url_by_source_id(conn, row["origin_source_id"])
    )
    if facts is None:
        facts = control_queries.lifecycle_facts(conn, row, controls=controls)
    task = control_queries.current_task(conn, facts["streams"])
    attempt = _latest_attempt(conn, task["task_id"]) if task else None
    execution = execution_summary(conn, task["task_id"]) if task else None
    pr = _derived_pr(conn, workstream_id) if workstream_id else None
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

    if row["canceled_at"]:
        column, reason_code, reason_summary = "history", "canceled", "Canceled by the operator"
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
    elif task and json.loads(task["metadata_json"] or "{}").get("audit_retry_required") and not running:
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
        and row["origin_type"] == "github"
        and not running
        and not publication_in_progress
        and not any(a["type"] == "operator_decision_required" for a in attention)
        and _waiting_github_context(conn, task["task_id"])
    ):
        column, reason_code, reason_summary = (
            "todo",
            "github_context_refresh_pending",
            "Queued; waiting for the background scanner to refresh GitHub context.",
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
    elif facts["source_closed"]:
        column, reason_code, reason_summary = (
            "waiting",
            "source_closed",
            "Source closed; cancel this item to finish its lifecycle.",
        )
    elif task and task["lifecycle"] in ACTIVE_TASK_LIFECYCLES:
        column, reason_code, reason_summary = "todo", task["lifecycle"], "Runnable work is queued"
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
        "review": review_store.board_status(conn, task["task_id"]) if task else None,
        "vote": vote_store.board_status(conn, task["task_id"]) if task else None,
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
    if not row["canceled_at"] and not row["completed_at"]:
        if facts["source_closed"] or facts["cleanup_pending"]:
            card["valid_commands"] = ["cancel"]
        elif (
            task
            and json.loads(task["metadata_json"] or "{}").get("execution_attention", {}).get("reason_code")
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
        elif task and json.loads(task["metadata_json"] or "{}").get("audit_retry_required") and not running:
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
                binding = execution_store.get_binding(conn, "task", task["task_id"])
                ownership_blocked = binding and binding["binding_status"] == BindingStatus.ATTENTION
                pending = db_records.first(db_read_wakeups.find_for_project_item(conn, task["task_id"]))
                operator_failure = json.loads(task["metadata_json"] or "{}").get("trigger_work_item_event_id") and any(
                    a["type"] == "execution_failed" and a["metadata"].get("task_id") == task["task_id"]
                    for a in attention
                )
                recovery_command = "retry" if operator_failure else "retrigger"
                card["valid_commands"] = ([] if pending or ownership_blocked else [recovery_command]) + ["cancel"]
            else:
                card["valid_commands"] = ["retry", "cancel"]
    if not facts["approval_task_id"] and any(a["type"] == "operator_decision_required" for a in attention):
        if not any(a["type"] == "completion_acceptance_requested" for a in attention):
            card["valid_commands"] = [command for command in card["valid_commands"] if command != "approve"]
    if column == "done" and facts["source_closed"]:
        card["valid_commands"] = []
    return card


def _all_projections(conn, *, repo=None, include_history=True):
    rows = work_item_queries.work_items_with_repositories(conn, repo=repo, include_history=include_history)
    controls = execution_store.read_controls(conn) if rows else {}
    return [_project_item(conn, row, controls=controls) for row in rows]


def workstream_projections(conn, *, workstream_ids=None, controls=None):
    """Share board lifecycle facts with the workbench without changing its buckets."""
    rows = work_item_queries.work_items_with_repositories(
        conn,
        workstream_ids=workstream_ids,
        attached_only=True,
    )
    projections = {}
    for row in rows:
        facts = control_queries.lifecycle_facts(conn, row, controls=controls)
        card = _project_item(conn, row, facts=facts)
        # This shared read model stays internal to the workbench. A canceled
        # owner must not hide unfinished execution in a derived workstream.
        card["operator_cancellation_settled"] = bool(row["canceled_at"]) and not any(
            facts[key] for key in ("running", "actions", "workflow_attention", "cleanup_pending")
        )
        projections[row["workstream_id"]] = card
    return projections


def _matches(item, *, repo, agent, priority, attention, query):
    if repo and repo not in {item["repo_id"], item["repo_full_name"]}:
        return False
    if agent and item["agent"] != agent:
        return False
    if priority and item["priority"] != priority:
        return False
    if attention is not None:
        wanted = attention in {True, "true", "1", "yes"}
        if (item["column"] == "waiting" or bool(item["attention_signals"])) != wanted:
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
    cursor_value = _cursor_decode(cursor)
    with closing(_connect(db_path)) as conn:
        projections = [
            item
            for item in _all_projections(conn, repo=repo, include_history=include_history)
            if _matches(
                item,
                repo=repo,
                agent=agent,
                priority=priority,
                attention=attention,
                query=query,
            )
            and (include_history or item["column"] != "history")
        ]
        counts = {
            column_id: sum(1 for item in projections if item["column"] == column_id)
            for column_id in [name for name, _label in COLUMNS] + ["history"]
        }
        if cursor_value:
            projections = [item for item in projections if (item["updated_at"], item["work_item_id"]) < cursor_value]
        if column:
            projections = [item for item in projections if item["column"] == column]
        page = projections[: limit + 1]
        has_more = len(page) > limit
        items = page[:limit]
        next_cursor = (
            _cursor_encode([items[-1]["updated_at"], items[-1]["work_item_id"]]) if has_more and items else None
        )
        running_rows = db_read_attempts.list_board(conn)
        per_repo_running = {row[0]: row[1] for row in running_rows}
        repos = [
            {"repo_id": row[0], "full_name": row[1], "running": per_repo_running.get(row[0], 0)}
            for row in db_read_repos.list_board(conn)
        ]
        agents = sorted({item["agent"] for item in projections} | {"Auto"})
    return {
        "columns": [{"id": column_id, "label": label, "count": counts[column_id]} for column_id, label in COLUMNS],
        "counts": counts,
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
