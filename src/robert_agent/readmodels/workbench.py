"""Scenario-specific SQLite read models for the DD GitHub workbench."""

from robert_agent.foundation.contracts.delivery import AuditStatus, PublishStatus
from robert_agent.foundation.contracts.execution import AttemptStatus, BindingStatus
from robert_agent.foundation.contracts.work import (
    ACTIVE_TASK_LIFECYCLES,
    RemediationStatus,
    TaskLifecycle,
    WorkstreamLifecycle,
)

import base64
import binascii
import json
import shlex
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from robert_agent.persistence import database as db_database
from robert_agent.persistence import errors as db_errors
from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import sqlite_master as db_read_sqlite_master
from robert_agent.persistence.queries import wakeups as db_read_wakeups
from robert_agent.persistence.queries import work_items as db_read_work_items
from robert_agent.persistence.queries import (
    workstream_sources as db_read_workstream_sources,
)
from robert_agent.persistence.queries.readviews import task_history
from robert_agent.persistence.queries.readviews import work_items as work_item_queries
from robert_agent.readmodels import board, execution_metrics
from robert_agent.readmodels.execution import safe_text

BUCKETS = {"needs_attention", "working", "waiting", "history"}
BUCKET_ORDER = {"needs_attention": 0, "working": 1, "waiting": 2, "history": 3}
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
SORTS = {"priority", "updated-desc", "updated-asc"}
REQUIRED_TABLES = {
    "repos",
    "github_sources",
    "workstreams",
    "tasks",
    "attempts",
    "worker_phases",
    "worker_results",
    "github_actions",
    "artifacts",
    "notifications",
    "wakeups",
    "execution_subjects",
}


class WorkItemQueryError(ValueError):
    """Raised for invalid workbench filters and cursors."""


def _decode_json(value, default=None):
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _connect(db_path):
    conn = db_database.connect(db_path)
    tables = db_read_sqlite_master.table_names(conn)
    missing = REQUIRED_TABLES - tables
    if missing:
        conn.close()
        raise db_errors.OperationalError("workbench schema is missing: " + ", ".join(sorted(missing)))
    return conn


def _encode_cursor(updated_at, workstream_id):
    raw = json.dumps([updated_at, workstream_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value):
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
        updated_at, workstream_id = json.loads(decoded)
    except (
        ValueError,
        TypeError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        raise WorkItemQueryError("invalid cursor") from exc
    if not isinstance(updated_at, str) or not isinstance(workstream_id, str):
        raise WorkItemQueryError("invalid cursor")
    return updated_at, workstream_id


def _group_by(rows, key):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row[key]].append(dict(row))
    return grouped


def _load_state(conn, workstream_id=None):
    # Ancestors provide the owning board item for a derived PR workstream.
    workstream_ids = None if workstream_id is None else work_item_queries.ancestor_workstream_ids(conn, workstream_id)
    workstreams = [dict(row) for row in work_item_queries.workstream_sources(conn, workstream_ids)]
    tasks = _group_by(task_history.tasks_for_workstreams(conn, workstream_ids), "workstream_id")
    task_ids = None if workstream_ids is None else [task["task_id"] for rows in tasks.values() for task in rows]
    attempts = _group_by(task_history.attempts_for_tasks(conn, task_ids), "task_id")
    attempt_ids = (
        None if task_ids is None else [attempt["attempt_id"] for rows in attempts.values() for attempt in rows]
    )
    phases = _group_by(task_history.phases_for_attempts(conn, attempt_ids), "attempt_id")
    results = _group_by(task_history.results_for_tasks(conn, task_ids), "task_id")
    actions = _group_by(task_history.actions_for_tasks(conn, task_ids), "task_id")
    artifacts = _group_by(task_history.artifacts_for_tasks(conn, task_ids), "task_id")
    notifications = _group_by(task_history.notifications_for_tasks(conn, task_ids), "task_id")
    remediation_episodes = _group_by(task_history.remediation_for_workstreams(conn, workstream_ids), "workstream_id")
    attention = {}
    from robert_agent.persistence import execution_control as execution_store

    subjects = set()
    for stream_tasks in tasks.values():
        for task in stream_tasks:
            subjects.add(("task", task["task_id"]))
            metadata = _decode_json(task.get("metadata_json"), {}) or {}
            if metadata.get("remediation_episode_id"):
                subjects.add(("remediation_episode", metadata["remediation_episode_id"]))
    controls = execution_store.read_controls(conn, subjects if workstream_ids is not None else None)
    for stream_tasks in tasks.values():
        for task in stream_tasks:
            metadata = _decode_json(task.get("metadata_json"), {}) or {}
            binding = controls.get(("task", task["task_id"])) or controls.get(
                ("remediation_episode", metadata.get("remediation_episode_id"))
            )
            if metadata.get("execution_attention"):
                attention[task["workstream_id"]] = safe_text(metadata["execution_attention"].get("reason_code"))
            if binding and binding["binding_status"] == BindingStatus.ATTENTION:
                metadata = _decode_json(binding["metadata_json"], {}) or {}
                attention[task["workstream_id"]] = safe_text(
                    metadata.get("reason_code") or "execution_attention_required"
                )
    occupied_tasks = task_history.occupied_tasks(conn, workstream_ids)
    return {
        "workflow_attention": attention,
        "occupied_tasks": occupied_tasks,
        "workstreams": workstreams,
        "workstreams_by_id": {row["workstream_id"]: row for row in workstreams},
        "board_items": board.workstream_projections(
            conn, workstream_ids=workstream_ids, controls=controls if workstream_ids is None else None
        )
        if db_read_sqlite_master.table_exists(conn, "work_items")
        else {},
        "tasks": tasks,
        "attempts": attempts,
        "phases": phases,
        "results": results,
        "actions": actions,
        "artifacts": artifacts,
        "notifications": notifications,
        "remediation_episodes": remediation_episodes,
    }


def _current_task(workstream, tasks):
    active_task_id = workstream.get("active_task_id")
    if active_task_id:
        for task in tasks:
            if task["task_id"] == active_task_id:
                return task
    return tasks[0] if tasks else None


def _publish_error(action):
    metadata = _decode_json(action.get("metadata_json"), {}) or {}
    publish = metadata.get("publish") if isinstance(metadata, dict) else None
    if not isinstance(publish, dict):
        return None
    return publish.get("safe_error") or publish.get("error")


def _derive_operator_state(
    workstream,
    task,
    attempt,
    results,
    actions,
    remediation_episodes,
):
    latest_by_kind = {}
    for episode in remediation_episodes:
        latest_by_kind.setdefault(episode["episode_kind"], episode)
    latest_episodes = list(latest_by_kind.values())
    remediation_superseded = bool(latest_episodes) and all(
        episode["status"] in {RemediationStatus.RESOLVED, RemediationStatus.SUPERSEDED, RemediationStatus.CANCELED}
        for episode in latest_episodes
    )
    failure = _decode_json((attempt or {}).get("failure_json"), {}) or {}
    if failure.get("reason_code") == "question_not_deliverable":
        return (
            "needs_attention",
            "question_not_deliverable",
            safe_text(failure.get("safe_error")) + " Confirm Worker cleanup, then correct the question and retry.",
        )
    for action in actions:
        if action["publish_status"] == PublishStatus.PUBLISHED:
            continue
        metadata = _decode_json(action.get("metadata_json"), {}) or {}
        publish = metadata.get("publish") if isinstance(metadata, dict) else None
        if isinstance(publish, dict) and publish.get("status") == "publish_failed":
            return (
                "needs_attention",
                "publish_failed",
                safe_text(
                    publish.get("required_action")
                    or (
                        f"Publication failed; retry at {publish['next_retry_at']}."
                        if publish.get("next_retry_at")
                        else "Publishing the accepted GitHub action failed."
                    )
                ),
            )
        if (
            action["audit_status"] == AuditStatus.ACCEPTED
            and action["publish_status"] == PublishStatus.SKIPPED
            and not remediation_superseded
        ):
            return (
                "needs_attention",
                "publish_skipped",
                "An accepted GitHub action was skipped during publication.",
            )
    exhausted = next(
        (episode for episode in latest_episodes if episode["status"] == RemediationStatus.EXHAUSTED),
        None,
    )
    if exhausted:
        metadata = _decode_json(exhausted.get("metadata_json"), {}) or {}
        reason = metadata.get("last_transition_reason") or "budget_exhausted"
        return (
            "needs_attention",
            "pr_remediation_exhausted",
            f"Automated PR remediation stopped: {reason}.",
        )
    if task and task["lifecycle"] in {TaskLifecycle.FAILED, TaskLifecycle.CANCELED}:
        return (
            "needs_attention",
            "task_failed" if task["lifecycle"] == TaskLifecycle.FAILED else "task_canceled",
            "The current task did not complete successfully.",
        )
    if attempt and attempt["status"] == AttemptStatus.STALE:
        return (
            "needs_attention",
            "worker_stale",
            "The current worker attempt stopped updating its heartbeat.",
        )
    if any(action["audit_status"] in {AuditStatus.FAILED, AuditStatus.POLICY_VIOLATION} for action in actions):
        return (
            "needs_attention",
            "github_action_rejected",
            "A planned GitHub action failed policy or audit checks.",
        )
    if workstream["workstream_lifecycle"] == WorkstreamLifecycle.FAILED:
        return (
            "needs_attention",
            "workstream_failed",
            "The GitHub workstream is in a failed state.",
        )
    if workstream["workstream_lifecycle"] == WorkstreamLifecycle.WAITING_FOR_USER or (
        task and task["lifecycle"] == TaskLifecycle.WAITING_FOR_USER
    ):
        return (
            "waiting",
            "waiting_for_user",
            "The agent is waiting for a trusted GitHub user response.",
        )
    if any(
        action["audit_status"] == AuditStatus.ACCEPTED and action["publish_status"] == PublishStatus.NOT_PUBLISHED
        for action in actions
    ):
        return (
            "working",
            "waiting_publish",
            "An accepted GitHub action is waiting for publication.",
        )
    if results:
        metadata = _decode_json(results[0].get("metadata_json"), {}) or {}
        if not isinstance(metadata, dict) or not metadata.get("audit"):
            return (
                "working",
                "result_pending_audit",
                "The worker result is waiting for the next audit cycle.",
            )
    if task and task["lifecycle"] in ACTIVE_TASK_LIFECYCLES:
        return (
            "working",
            "agent_working",
            "The agent is actively processing this GitHub work item.",
        )
    if workstream["workstream_lifecycle"] == WorkstreamLifecycle.ACTIVE:
        return (
            "working",
            "active_workstream",
            "The GitHub workstream remains active.",
        )
    if any(
        episode["status"] in {RemediationStatus.OPEN, RemediationStatus.QUEUED, RemediationStatus.REMEDIATING}
        for episode in latest_episodes
    ):
        return (
            "working",
            "pr_remediation_active",
            "Automated PR remediation is active.",
        )
    if any(episode["status"] == RemediationStatus.WAITING_FOR_SIGNAL for episode in latest_episodes):
        return (
            "waiting",
            "pr_remediation_waiting_for_signal",
            "A remediation push is waiting for the next PR health signal.",
        )
    return (
        "history",
        "inactive",
        "The GitHub workstream no longer requires active attention.",
    )


def _source_payload(workstream):
    return {
        "source_id": workstream.get("primary_source_id"),
        "repo_id": workstream.get("repo_id"),
        "repo_full_name": workstream.get("repo_full_name"),
        "type": workstream.get("source_type"),
        "number": workstream.get("source_number"),
        "title": workstream.get("source_title") or "Untitled GitHub work item",
        "url": workstream.get("github_url"),
        "state": workstream.get("source_state"),
        "author_login": workstream.get("author_login"),
        "updated_at": workstream.get("source_updated_at"),
        "source_key": workstream.get("source_key"),
    }


def _phase_for_attempt(state, attempt):
    if not attempt:
        return None
    phases = state["phases"].get(attempt["attempt_id"], [])
    return phases[-1]["phase"] if phases else None


def _item_from_workstream(state, workstream):
    tasks = state["tasks"].get(workstream["workstream_id"], [])
    task = _current_task(workstream, tasks)
    task_id = task["task_id"] if task else None
    task_attempts = state["attempts"].get(task_id, []) if task_id else []
    attempt = task_attempts[0] if task_attempts else None
    results = state["results"].get(task_id, []) if task_id else []
    actions = state["actions"].get(task_id, []) if task_id else []
    notifications = state["notifications"].get(task_id, []) if task_id else []
    remediation_episodes = state["remediation_episodes"].get(
        workstream["workstream_id"],
        [],
    )
    bucket, reason_code, reason_summary = _derive_operator_state(
        workstream,
        task,
        attempt,
        results,
        actions,
        remediation_episodes,
    )
    if workstream["workstream_id"] in state.get("workflow_attention", {}):
        bucket, reason_code, reason_summary = (
            "needs_attention",
            state["workflow_attention"][workstream["workstream_id"]],
            "Automatic execution stopped. Inspect the failure, resolve its cause, then explicitly retry or cancel.",
        )
    board_item = state["board_items"].get(workstream["workstream_id"])
    ancestor = workstream
    seen = set()
    streams = state["workstreams_by_id"]
    while board_item is None and ancestor.get("origin_workstream_id") not in seen:
        origin = ancestor.get("origin_workstream_id")
        if not origin:
            break
        seen.add(origin)
        board_item = state["board_items"].get(origin)
        ancestor = streams.get(origin, {})
    lifecycle = (
        {key: board_item[key] for key in ("work_item_id", "column", "reason_code", "reason_summary", "pr")}
        if board_item
        else None
    )
    if (
        workstream["workstream_lifecycle"] in {WorkstreamLifecycle.COMPLETED, WorkstreamLifecycle.CANCELED}
        and (
            str(workstream.get("source_state") or "").lower() in {"closed", "merged"}
            or (board_item and board_item["operator_cancellation_settled"])
        )
        and (not board_item or board_item["reason_code"] != "canceled" or board_item["operator_cancellation_settled"])
        and workstream["workstream_id"] not in state.get("workflow_attention", {})
        and not any(t["lifecycle"] in ACTIVE_TASK_LIFECYCLES | {"waiting_for_user"} for t in tasks)
        and not any(t["task_id"] in state["occupied_tasks"] for t in tasks)
        and not any(
            a["audit_status"] in {AuditStatus.PENDING, AuditStatus.ACCEPTED}
            and a["publish_status"] == PublishStatus.NOT_PUBLISHED
            for t in tasks
            for a in state["actions"].get(t["task_id"], [])
        )
        and not any(
            e["status"] in {"open", "queued", "remediating", "waiting_for_signal"} for e in remediation_episodes
        )
    ):
        if board_item and board_item["reason_code"] == "canceled":
            bucket, reason_code, reason_summary = "history", "canceled", "Canceled by the operator"
        else:
            bucket, reason_code, reason_summary = (
                "history",
                "inactive",
                "The source is closed and its workstream has ended; previous failures remain in execution history.",
            )
    failed_publish_actions = sum(
        1 for action in actions if action["publish_status"] != PublishStatus.PUBLISHED and _publish_error(action)
    )
    item = {
        "id": workstream["workstream_id"],
        "workstream_lifecycle": workstream["workstream_lifecycle"],
        "lifecycle": lifecycle,
        "bucket": bucket,
        "reason_code": reason_code,
        "reason_summary": reason_summary,
        "priority": task["priority"] if task else "P2",
        "updated_at": (task or workstream).get("updated_at"),
        "source": _source_payload(workstream),
        "agent": {
            "task_id": task_id,
            "route_id": task.get("route_id") if task else None,
            "attempt_id": attempt.get("attempt_id") if attempt else None,
            "attempt_no": attempt.get("attempt_no") if attempt else None,
            "attempt_status": attempt.get("status") if attempt else None,
            "phase": _phase_for_attempt(state, attempt),
            "heartbeat_at": attempt.get("heartbeat_at") if attempt else None,
        },
        "signals": {
            "result_pending_audit": reason_code == "result_pending_audit",
            "pending_publish_actions": sum(
                1
                for action in actions
                if action["audit_status"] == AuditStatus.ACCEPTED
                and action["publish_status"] == PublishStatus.NOT_PUBLISHED
            ),
            "failed_publish_actions": failed_publish_actions,
            "worker_resume_count": sum(
                1 for notification in notifications if notification["notification_type"] == "worker_resume_prepared"
            ),
            "remediation_status": (remediation_episodes[0]["status"] if remediation_episodes else None),
        },
        "task_ids": [candidate["task_id"] for candidate in tasks],
    }
    return item


def _parse_query(query):
    qualifiers = defaultdict(list)
    terms = []
    try:
        tokens = shlex.split(query or "")
    except ValueError as exc:
        raise WorkItemQueryError("invalid query") from exc
    for token in tokens:
        if ":" in token:
            name, value = token.split(":", 1)
            if name in {"repo", "is", "author", "task"} and value:
                qualifiers[name].append(value.lower())
                continue
        terms.append(token.lower())
    return qualifiers, terms


def _text_contains_all(values, terms):
    text = " ".join(str(value or "") for value in values).lower()
    return all(term in text for term in terms)


def _matches_query(item, qualifiers, terms):
    source = item["source"]
    if qualifiers["repo"] and not any(
        value
        in {
            str(source.get("repo_id") or "").lower(),
            str(source.get("repo_full_name") or "").lower(),
        }
        for value in qualifiers["repo"]
    ):
        return False
    if qualifiers["author"] and str(source.get("author_login") or "").lower() not in qualifiers["author"]:
        return False
    if qualifiers["task"] and not all(
        any(value in task_id.lower() for task_id in item["task_ids"]) for value in qualifiers["task"]
    ):
        return False
    for value in qualifiers["is"]:
        if value == "active" and item["bucket"] == "history":
            return False
        if value in {"attention", "needs_attention"} and item["bucket"] != "needs_attention":
            return False
        if value in {"working", "waiting", "history"} and item["bucket"] != value:
            return False
        if value in {"open", "closed"} and str(source.get("state") or "").lower() != value:
            return False
    return _text_contains_all(
        [
            source.get("title"),
            source.get("number"),
            source.get("source_key"),
            *item["task_ids"],
        ],
        terms,
    )


def _sort_items(items, sort):
    if sort == "updated-asc":
        return sorted(items, key=lambda item: (item["updated_at"] or "", item["id"]))
    if sort == "updated-desc":
        return sorted(
            items,
            key=lambda item: (item["updated_at"] or "", item["id"]),
            reverse=True,
        )
    newest_first = sorted(
        items,
        key=lambda item: (item["updated_at"] or "", item["id"]),
        reverse=True,
    )
    return sorted(
        newest_first,
        key=lambda item: (
            BUCKET_ORDER[item["bucket"]],
            PRIORITY_ORDER.get(item["priority"], 99),
        ),
    )


def list_work_items(
    db_path,
    *,
    bucket=None,
    repo=None,
    actor=None,
    query="",
    sort="priority",
    limit=30,
    cursor=None,
):
    """Return a filtered and paginated PR/Issue-first work list."""

    if bucket and bucket not in BUCKETS:
        raise WorkItemQueryError("invalid bucket")
    if sort not in SORTS:
        raise WorkItemQueryError("invalid sort")
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise WorkItemQueryError("limit must be between 1 and 100")
    qualifiers, terms = _parse_query(query)
    with closing(_connect(db_path)) as conn:
        state = _load_state(conn)
    items = [_item_from_workstream(state, workstream) for workstream in state["workstreams"]]
    repositories = sorted(
        {
            (item["source"]["repo_id"], item["source"]["repo_full_name"])
            for item in items
            if item["source"]["repo_id"] and item["source"]["repo_full_name"]
        },
        key=lambda value: (value[1].lower(), value[0]),
    )
    actors = sorted(
        {item["source"]["author_login"] for item in items if item["source"]["author_login"]},
        key=str.lower,
    )
    items = [
        item
        for item in items
        if (not repo or repo in {item["source"]["repo_id"], item["source"]["repo_full_name"]})
        and (not actor or actor == item["source"]["author_login"])
        and _matches_query(item, qualifiers, terms)
    ]
    counts = Counter(item["bucket"] for item in items)
    if bucket:
        items = [item for item in items if item["bucket"] == bucket]
    elif not qualifiers["is"]:
        items = [item for item in items if item["bucket"] != "history"]
    items = _sort_items(items, sort)
    if cursor:
        cursor_pair = _decode_cursor(cursor)
        try:
            cursor_index = next(
                index for index, item in enumerate(items) if (item["updated_at"] or "", item["id"]) == cursor_pair
            )
        except StopIteration as exc:
            raise WorkItemQueryError("invalid cursor") from exc
        items = items[cursor_index + 1 :]
    page = items[:limit]
    next_cursor = None
    if len(items) > limit and page:
        next_cursor = _encode_cursor(page[-1]["updated_at"] or "", page[-1]["id"])
    for item in page:
        item.pop("task_ids", None)
    return {
        "items": page,
        "counts": {name: counts.get(name, 0) for name in BUCKET_ORDER},
        "repositories": [{"repo_id": repo_id, "full_name": full_name} for repo_id, full_name in repositories],
        "actors": actors,
        "next_cursor": next_cursor,
    }


def _safe_failure(value):
    failure = _decode_json(value, {}) or {}
    if not isinstance(failure, dict):
        return None
    return safe_text(
        failure.get("safe_error")
        or failure.get("message")
        or failure.get("reason")
        or failure.get("original_status")
        or failure.get("status")
    )


def _safe_result(row):
    metadata = _decode_json(row.get("metadata_json"), {}) or {}
    audit = metadata.get("audit") if isinstance(metadata, dict) else None
    usage = metadata.get("usage") if isinstance(metadata, dict) else None
    payload = {
        "result_id": row["result_id"],
        "task_id": row["task_id"],
        "attempt_id": row["attempt_id"],
        "output_type": row["output_type"],
        "verification": _decode_json(row.get("verification_json"), []) or [],
        "handoff": row.get("handoff") or "",
        "created_at": row["created_at"],
        "audit_status": audit.get("status") if isinstance(audit, dict) else None,
    }
    if isinstance(usage, dict):
        payload["usage"] = {
            key: usage.get(key) for key in ("usage_available", "source", "usage", "total_cost_usd") if key in usage
        }
    return payload


def _safe_action(row):
    metadata = _decode_json(row.get("metadata_json"), {}) or {}
    publish = metadata.get("publish") if isinstance(metadata, dict) else None
    return {
        "action_id": row["action_id"],
        "result_id": row["result_id"],
        "task_id": row["task_id"],
        "action_type": row["action_type"],
        "target_url": row["target_url"],
        "external_id": row["external_id"],
        "audit_status": row["audit_status"],
        "publish_status": row["publish_status"],
        "safe_error": safe_text(publish.get("safe_error")) if isinstance(publish, dict) else None,
        "retry": {
            key: publish.get(key)
            for key in ("attempt_count", "max_attempts", "reason_code", "next_retry_at", "required_action")
        }
        if isinstance(publish, dict)
        else {},
        "created_at": row["created_at"],
    }


def get_work_item(db_path, workstream_id):
    """Return the structured detail for one workstream."""

    with closing(_connect(db_path)) as conn:
        state = _load_state(conn, workstream_id)
        workstream = next(
            (candidate for candidate in state["workstreams"] if candidate["workstream_id"] == workstream_id),
            None,
        )
        if not workstream:
            return None
        item = _item_from_workstream(state, workstream)
        board_item = None
        if db_read_sqlite_master.table_exists(conn, "work_items"):
            board_item = db_records.first(
                db_read_work_items.find_for_get_work_item(
                    conn, workstream_id, workstream.get("origin_workstream_id"), workstream_id
                )
            )
        related_sources = [
            {
                "relationship": row["relationship"],
                "source_id": row["source_id"],
                "source_key": row["source_key"],
                "type": row["source_type"],
                "number": row["number"],
                "title": row["title"],
                "url": row["html_url"],
                "state": row["state"],
            }
            for row in db_read_workstream_sources.find_for_get_work_item(conn, workstream_id)
        ]
        current_task = _current_task(
            workstream,
            state["tasks"].get(workstream_id, []),
        )
        timings = [
            execution_metrics.task_timing(conn, task["task_id"]) for task in state["tasks"].get(workstream_id, [])
        ]
        retrigger_pending = False
        if current_task:
            retrigger_pending = bool(
                db_records.first(db_read_wakeups.find_for_get_work_item(conn, current_task["task_id"]))
            )
    tasks = state["tasks"].get(workstream_id, [])
    task_ids = {task["task_id"] for task in tasks}
    attempts = [row for task_id in task_ids for row in state["attempts"].get(task_id, [])]
    attempt_ids = {attempt["attempt_id"] for attempt in attempts}
    phases = [row for attempt_id in attempt_ids for row in state["phases"].get(attempt_id, [])]
    results = [row for task_id in task_ids for row in state["results"].get(task_id, [])]
    actions = [row for task_id in task_ids for row in state["actions"].get(task_id, [])]
    artifacts = [row for task_id in task_ids for row in state["artifacts"].get(task_id, [])]
    return {
        "id": workstream_id,
        "work_item_id": item["lifecycle"]["work_item_id"]
        if item["lifecycle"]
        else (board_item[0] if board_item else None),
        "source": item["source"],
        "workstream_lifecycle": item["workstream_lifecycle"],
        "lifecycle": item["lifecycle"],
        "related_sources": related_sources,
        "execution_timings": timings,
        "notification_delivery": [
            {
                "notification_id": row["notification_id"],
                "type": row["notification_type"],
                "created_at": row["created_at"],
                "delivery": _decode_json(row["metadata_json"]).get("delivery", {"status": "pending"}),
            }
            for task_id in task_ids
            for row in state["notifications"].get(task_id, [])
        ],
        "operator_state": {
            "bucket": item["bucket"],
            "reason_code": item["reason_code"],
            "reason_summary": item["reason_summary"],
        },
        "controls": {
            "can_retrigger": bool(
                current_task
                and (
                    (
                        current_task["lifecycle"] == TaskLifecycle.FAILED
                        and workstream["workstream_lifecycle"] == WorkstreamLifecycle.FAILED
                    )
                    or _decode_json(current_task.get("metadata_json"), {})
                    .get("execution_attention", {})
                    .get("reason_code")
                    == "result_contract_cutover"
                )
                and str(workstream.get("source_state") or "").lower() == "open"
                and not retrigger_pending
            ),
            "retrigger_pending": retrigger_pending,
            "task_id": current_task["task_id"] if current_task else None,
        },
        "tasks": [
            {
                "task_id": row["task_id"],
                "parent_task_id": row["parent_task_id"],
                "lifecycle": row["lifecycle"],
                "priority": row["priority"],
                "route_id": row["route_id"],
                "expected_output": row["expected_output"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in sorted(tasks, key=lambda row: (row["created_at"], row["task_id"]))
        ],
        "attempts": [
            {
                "attempt_id": row["attempt_id"],
                "task_id": row["task_id"],
                "attempt_no": row["attempt_no"],
                "status": row["status"],
                "worktree_path": row["worktree_path"],
                "branch_name": row["branch_name"],
                "started_at": row["started_at"],
                "heartbeat_at": row["heartbeat_at"],
                "finished_at": row["finished_at"],
                "failure_summary": _safe_failure(row["failure_json"]),
            }
            for row in attempts
        ],
        "phases": phases,
        "results": [_safe_result(row) for row in results],
        "actions": [_safe_action(row) for row in actions],
        "artifacts": [
            {
                "artifact_id": row["artifact_id"],
                "task_id": row["task_id"],
                "attempt_id": row["attempt_id"],
                "artifact_type": row["artifact_type"],
                "bytes": row["bytes"],
                "created_at": row["created_at"],
                "preview_available": Path(row["path"]).expanduser().is_file(),
            }
            for row in artifacts
        ],
    }
