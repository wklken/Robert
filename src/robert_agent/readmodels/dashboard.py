"""Read-only dashboard and artifact projections, independent of HTTP."""

from collections import Counter
from contextlib import closing
import json
import os
import stat
import sqlite3
from pathlib import Path
from robert_agent.readmodels import summarize
from robert_agent.execution import runtime_state as daemon_state
from robert_agent.context import knowledge as runtime_knowledge
from robert_agent.readmodels import usage
from robert_agent.work import wakeup
from robert_agent.readmodels.history import step_output


RUN_STEP_ORDER = [
    "validate_config",
    "discover_notifications",
    "acquire_lease",
    "supervise",
    "audit_results",
    "publish_actions",
    "reconcile",
    "discover",
    "authorize",
    "route",
    "repo_pipelines",
    "dispatch",
    "summarize",
]

REPO_STEP_ORDER = [
    "acquire_lease",
    "supervise",
    "audit_results",
    "publish_actions",
    "reconcile",
    "discover",
    "authorize",
    "route",
]


def _decode_json(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _table_exists(conn, table_name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _load_latest_loop(db_path):
    path = Path(db_path).parent / "latest-loop.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_authorized_unattached_event(event):
    return (
        event["authorization_status"]
        in {
            "authorized",
            "authorized_trigger",
            "accepted_context",
            "accepted_review_participant",
        }
        and not event["task_id"]
    )


def _task_operator_state(task):
    if task.get("execution_attention"):
        return "needs_attention", "inspect_execution_attention"
    if task["lifecycle"] in {"failed", "canceled"}:
        notification = task.get("latest_notification") or {}
        if notification.get("notification_type") == "worker_timeout":
            return "needs_attention", "inspect_worker_timeout"
        if notification.get("notification_type") == "worker_startup_failed":
            return "needs_attention", "inspect_worker_startup"
        return "needs_attention", "inspect_failure_notification"
    if task.get("failed_publish_actions", 0):
        return "needs_attention", "inspect_github_publish_failure"
    if task.get("skipped_publish_actions", 0):
        return "needs_attention", "inspect_github_publish_skipped"
    if task.get("failed_github_actions", 0):
        return "needs_attention", "inspect_failure_notification"
    if task.get("pending_publish_actions", 0):
        return "waiting_publish", "publish_accepted_github_actions"
    if task["lifecycle"] == "waiting_for_user" or task.get("workstream_lifecycle") == "waiting_for_user":
        return "waiting_user", "wait_for_trusted_user_reply"
    if task.get("latest_attempt_status") == "stale":
        return "worker_stale", "inspect_worker_heartbeat"
    if task["lifecycle"] in {"queued", "running"} and task.get("worker_resume_count", 0) >= 2:
        return "worker_retrying", "inspect_worker_resume"
    if task["lifecycle"] in {"queued", "running"} and task.get("result_pending_audit"):
        return "result_pending_audit", "wait_next_cycle_or_run_once"
    if task["lifecycle"] in {"queued", "running"} and task.get("latest_attempt_status") == "running":
        return "worker_running", "wait_for_worker_completion"
    if task["lifecycle"] in {
        "detected",
        "authorized",
        "classified",
        "queued",
        "running",
    }:
        return "waiting_worker", "wait_for_worker_or_supervise"
    if task["lifecycle"] == "completed":
        return "completed", "none"
    if task["lifecycle"] == "ignored":
        return "ignored", "none"
    return "unknown", "inspect_task"


def _attach_task_operator_state(conn, recent_tasks):
    if not recent_tasks:
        return
    task_ids = [task["task_id"] for task in recent_tasks]
    placeholders = ",".join("?" for _task_id in task_ids)
    actions_by_task = {
        task_id: {
            "pending_publish_actions": 0,
            "failed_github_actions": 0,
            "failed_publish_actions": 0,
            "skipped_publish_actions": 0,
            "latest_pending_publish": None,
            "latest_publish_failure": None,
            "latest_publish_skipped": None,
        }
        for task_id in task_ids
    }
    for row in conn.execute(
        f"""
        SELECT
          task_id, action_id, action_type, target_url,
          audit_status, publish_status, created_at, metadata_json
        FROM github_actions
        WHERE task_id IN ({placeholders})
        ORDER BY created_at DESC, action_id DESC
        """,
        task_ids,
    ):
        action_state = actions_by_task[row["task_id"]]
        if row["audit_status"] == "accepted" and row["publish_status"] == "not_published":
            action_state["pending_publish_actions"] += 1
            if not action_state["latest_pending_publish"]:
                action_state["latest_pending_publish"] = {
                    "action_id": row["action_id"],
                    "action_type": row["action_type"],
                    "target_url": row["target_url"],
                }
        if row["audit_status"] in {"failed", "policy_violation"}:
            action_state["failed_github_actions"] += 1
        metadata = _decode_json(row["metadata_json"]) or {}
        publish = metadata.get("publish") if isinstance(metadata, dict) else {}
        if row["audit_status"] == "accepted" and row["publish_status"] == "skipped":
            action_state["skipped_publish_actions"] += 1
            if not action_state["latest_publish_skipped"]:
                action_state["latest_publish_skipped"] = {
                    "action_id": row["action_id"],
                    "action_type": row["action_type"],
                    "target_url": row["target_url"],
                    "safe_error": publish.get("safe_error") if isinstance(publish, dict) else None,
                    "command": publish.get("command") if isinstance(publish, dict) else None,
                }
        if isinstance(publish, dict) and publish.get("status") == "publish_failed":
            action_state["failed_publish_actions"] += 1
            if not action_state["latest_publish_failure"]:
                action_state["latest_publish_failure"] = {
                    "action_id": row["action_id"],
                    "action_type": row["action_type"],
                    "target_url": row["target_url"],
                    "safe_error": publish.get("safe_error"),
                    "command": publish.get("command"),
                }

    attempts_by_task = {}
    for row in conn.execute(
        f"""
        SELECT task_id, attempt_id, status, heartbeat_at, started_at, finished_at
        FROM attempts
        WHERE task_id IN ({placeholders})
        ORDER BY attempt_no DESC, started_at DESC, attempt_id DESC
        """,
        task_ids,
    ):
        attempts_by_task.setdefault(row["task_id"], dict(row))

    notifications_by_task = {}
    worker_resume_counts_by_task = {task_id: 0 for task_id in task_ids}
    for row in conn.execute(
        f"""
        SELECT notification_id, task_id, notification_type, channel, status,
               created_at, metadata_json
        FROM notifications
        WHERE task_id IN ({placeholders})
        ORDER BY created_at DESC, notification_id DESC
        """,
        task_ids,
    ):
        notification = dict(row)
        notification["metadata"] = _decode_json(notification.pop("metadata_json"))
        if notification["notification_type"] == "worker_resume_prepared":
            worker_resume_counts_by_task[row["task_id"]] += 1
        notifications_by_task.setdefault(row["task_id"], notification)

    result_pending_audit_by_task = {task_id: False for task_id in task_ids}
    for row in conn.execute(
        f"""
        SELECT wr.task_id, wr.metadata_json
        FROM worker_results wr
        JOIN attempts a ON a.attempt_id = wr.attempt_id
        WHERE wr.task_id IN ({placeholders})
          AND a.status = 'completed'
        ORDER BY wr.created_at DESC, wr.result_id DESC
        """,
        task_ids,
    ):
        metadata = _decode_json(row["metadata_json"]) or {}
        if isinstance(metadata, dict) and not metadata.get("audit"):
            result_pending_audit_by_task[row["task_id"]] = True

    sources_by_task = {}
    for row in conn.execute(
        f"""
        SELECT
          te.task_id,
          ge.event_fingerprint,
          gs.source_key,
          gs.title AS source_title,
          gs.html_url,
          gs.source_type,
          gs.number
        FROM task_events te
        JOIN github_events ge ON ge.event_id = te.event_id
        JOIN github_sources gs ON gs.source_id = ge.source_id
        WHERE te.task_id IN ({placeholders})
        ORDER BY
          CASE te.relationship
            WHEN 'trigger' THEN 0
            WHEN 'context' THEN 1
            WHEN 'pending' THEN 2
            WHEN 'consumed' THEN 3
            ELSE 4
          END,
          te.created_at DESC
        """,
        task_ids,
    ):
        sources_by_task.setdefault(row["task_id"], dict(row))

    for row in conn.execute(
        f"""
        SELECT t.task_id, gs.source_key, gs.title AS source_title, gs.html_url,
               gs.source_type, gs.number
        FROM tasks t
        JOIN workstreams w ON w.workstream_id = t.workstream_id
        JOIN github_sources gs ON gs.source_id = w.primary_source_id
        WHERE t.task_id IN ({placeholders})
        """,
        task_ids,
    ):
        sources_by_task.setdefault(row["task_id"], dict(row))

    artifacts_by_task = {task_id: {} for task_id in task_ids}
    for row in conn.execute(
        f"""
        SELECT task_id, artifact_type, path, bytes, created_at
        FROM artifacts
        WHERE task_id IN ({placeholders})
        ORDER BY created_at DESC, artifact_id DESC
        """,
        task_ids,
    ):
        artifacts_by_task[row["task_id"]].setdefault(
            row["artifact_type"],
            {
                "path": row["path"],
                "bytes": row["bytes"],
            },
        )

    attention_by_task = {
        row["task_id"]: (_decode_json(row["metadata_json"]) or {}).get("execution_attention")
        for row in conn.execute(
            f"SELECT task_id,metadata_json FROM tasks WHERE task_id IN ({placeholders})",
            task_ids,
        )
    }
    for task in recent_tasks:
        task["execution_attention"] = attention_by_task.get(task["task_id"])
        task.update(actions_by_task.get(task["task_id"], {}))
        latest_attempt = attempts_by_task.get(task["task_id"]) or {}
        task["latest_attempt_id"] = latest_attempt.get("attempt_id")
        task["latest_attempt_status"] = latest_attempt.get("status")
        task["latest_attempt_heartbeat_at"] = latest_attempt.get("heartbeat_at")
        task["latest_attempt_started_at"] = latest_attempt.get("started_at")
        task["latest_attempt_finished_at"] = latest_attempt.get("finished_at")
        task["latest_notification"] = notifications_by_task.get(task["task_id"])
        task["worker_resume_count"] = worker_resume_counts_by_task.get(task["task_id"], 0)
        task["result_pending_audit"] = result_pending_audit_by_task.get(task["task_id"], False)
        source = sources_by_task.get(task["task_id"]) or {}
        task["source_key"] = source.get("source_key")
        task["source_title"] = source.get("source_title")
        task["github_url"] = source.get("html_url")
        task["source_type"] = source.get("source_type")
        task["source_number"] = source.get("number")
        task["trigger_event_fingerprint"] = source.get("event_fingerprint")
        task["artifacts"] = dict(sorted(artifacts_by_task.get(task["task_id"], {}).items()))
        operator_state, next_action = _task_operator_state(task)
        task["operator_state"] = operator_state
        task["next_action"] = next_action


def _build_operator_alerts(operator_state_counts, event_flow_counts, summary_counts):
    alerts = []

    def add(alert_type, count, severity, next_action):
        if count:
            alerts.append(
                {
                    "alert_type": alert_type,
                    "count": count,
                    "severity": severity,
                    "next_action": next_action,
                }
            )

    add(
        "needs_attention_tasks",
        operator_state_counts.get("needs_attention", 0),
        "critical",
        "inspect_attention_tasks",
    )
    add(
        "worker_stale_tasks",
        operator_state_counts.get("worker_stale", 0),
        "warning",
        "inspect_worker_heartbeat",
    )
    add(
        "worker_retrying_tasks",
        operator_state_counts.get("worker_retrying", 0),
        "warning",
        "inspect_worker_resume",
    )
    add(
        "result_pending_audit_tasks",
        operator_state_counts.get("result_pending_audit", 0),
        "warning",
        "wait_next_cycle_or_run_once",
    )
    add(
        "authorized_unattached_events",
        event_flow_counts.get("authorized_unattached", 0),
        "warning",
        "inspect_recent_events",
    )
    add(
        "pending_actor_permission_events",
        summary_counts.get(
            "pending_actor_permission_events",
            event_flow_counts.get("authorization_status", {}).get("pending_actor_permission", 0),
        ),
        "warning",
        "resolve_actor_permission",
    )
    add(
        "pending_authorization_events",
        summary_counts.get(
            "pending_authorization_events",
            event_flow_counts.get("authorization_status", {}).get("pending_authorization", 0),
        ),
        "warning",
        "resolve_authorization_lookup",
    )
    add(
        "waiting_publish_tasks",
        operator_state_counts.get("waiting_publish", 0),
        "info",
        "publish_accepted_github_actions",
    )
    return alerts


def _event_operator_next_step(event):
    if _is_authorized_unattached_event(event):
        return {
            "target_kind": "event",
            "target_id": event["event_id"],
            "github_url": event.get("html_url"),
            "source_key": event.get("source_key"),
            "source_title": event.get("source_title"),
            "event_fingerprint": event.get("event_fingerprint"),
            "actor_login": event.get("actor_login"),
            "severity": "warning",
            "next_action": "inspect_recent_events",
            "reason": "authorized_unattached",
        }
    if event["authorization_status"] == "pending_actor_permission":
        return {
            "target_kind": "event",
            "target_id": event["event_id"],
            "github_url": event.get("html_url"),
            "source_key": event.get("source_key"),
            "source_title": event.get("source_title"),
            "event_fingerprint": event.get("event_fingerprint"),
            "actor_login": event.get("actor_login"),
            "severity": "warning",
            "next_action": "resolve_actor_permission",
            "reason": "pending_actor_permission",
        }
    if event["authorization_status"] == "pending_authorization":
        return {
            "target_kind": "event",
            "target_id": event["event_id"],
            "github_url": event.get("html_url"),
            "source_key": event.get("source_key"),
            "source_title": event.get("source_title"),
            "event_fingerprint": event.get("event_fingerprint"),
            "actor_login": event.get("actor_login"),
            "severity": "warning",
            "next_action": "resolve_authorization_lookup",
            "reason": "pending_authorization",
        }
    return None


def _build_operator_next_steps(recent_tasks, recent_events, priority_events=None):
    steps = []
    task_severities = {
        "needs_attention": "critical",
        "worker_stale": "warning",
        "worker_retrying": "warning",
        "result_pending_audit": "warning",
        "waiting_publish": "info",
    }
    for task in recent_tasks:
        severity = task_severities.get(task["operator_state"])
        if not severity:
            continue
        step = {
            "target_kind": "task",
            "target_id": task["task_id"],
            "github_url": task.get("github_url"),
            "source_key": task.get("source_key"),
            "source_title": task.get("source_title"),
            "workstream_id": task["workstream_id"],
            "route_id": task.get("route_id"),
            "severity": severity,
            "next_action": task["next_action"],
            "reason": task["operator_state"],
        }
        if task["next_action"] == "inspect_github_publish_failure":
            publish_failure = task.get("latest_publish_failure") or {}
            step["notification_type"] = "github_publish_failed"
            step["safe_error"] = publish_failure.get("safe_error")
            step["command"] = publish_failure.get("command")
        elif task["next_action"] == "inspect_github_publish_skipped":
            publish_skipped = task.get("latest_publish_skipped") or {}
            step["notification_type"] = "github_publish_skipped"
            step["safe_error"] = publish_skipped.get("safe_error")
            step["command"] = publish_skipped.get("command")
        elif task["next_action"] == "publish_accepted_github_actions":
            pending_publish = task.get("latest_pending_publish") or {}
            step["action_id"] = pending_publish.get("action_id")
            step["action_type"] = pending_publish.get("action_type")
            step["target_url"] = pending_publish.get("target_url")
        elif task["next_action"] == "inspect_failure_notification":
            notification = task.get("latest_notification") or {}
            metadata = notification.get("metadata") or {}
            audit = metadata.get("audit") if isinstance(metadata, dict) else {}
            step["notification_type"] = notification.get("notification_type")
            step["result_id"] = metadata.get("result_id") if isinstance(metadata, dict) else None
            step["safe_error"] = (
                audit.get("safe_error")
                if isinstance(audit, dict)
                else metadata.get("safe_error")
                if isinstance(metadata, dict)
                else None
            )
        elif task["next_action"] in {
            "inspect_worker_heartbeat",
            "inspect_worker_timeout",
            "inspect_worker_startup",
        }:
            step["attempt_id"] = task.get("latest_attempt_id")
            step["attempt_status"] = task.get("latest_attempt_status")
            step["heartbeat_at"] = task.get("latest_attempt_heartbeat_at")
            step["started_at"] = task.get("latest_attempt_started_at")
            step["finished_at"] = task.get("latest_attempt_finished_at")
        elif task["next_action"] == "inspect_worker_resume":
            notification = task.get("latest_notification") or {}
            metadata = notification.get("metadata") or {}
            step["notification_type"] = notification.get("notification_type")
            step["attempt_id"] = metadata.get("attempt_id") if isinstance(metadata, dict) else None
            step["resume_attempt_id"] = metadata.get("resume_attempt_id") if isinstance(metadata, dict) else None
            step["recovery_artifact_path"] = (
                metadata.get("recovery_artifact_path") if isinstance(metadata, dict) else None
            )
            step["worker_resume_count"] = task.get("worker_resume_count")
        elif task["next_action"] == "wait_next_cycle_or_run_once":
            step["attempt_id"] = task.get("latest_attempt_id")
            step["attempt_status"] = task.get("latest_attempt_status")
        steps.append(step)
    seen_event_ids = set()
    for event in list(recent_events) + list(priority_events or []):
        if event["event_id"] in seen_event_ids:
            continue
        seen_event_ids.add(event["event_id"])
        step = _event_operator_next_step(event)
        if step:
            steps.append(step)
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(
        steps,
        key=lambda step: (
            severity_order.get(step["severity"], 99),
            step["target_kind"],
            step["target_id"],
        ),
    )


def _notification_metadata(value):
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    return {"value": value}


def _metadata_task_id(metadata):
    if isinstance(metadata, dict):
        return metadata.get("task_id")
    return None


def _notification_task_id(notification):
    metadata = notification.get("metadata") or {}
    return notification.get("task_id") or _metadata_task_id(metadata)


def _status_severity(*values):
    text = " ".join(str(value or "").lower() for value in values)
    if any(token in text for token in ("failed", "failure", "rejected", "timeout", "error")):
        return "critical"
    if any(token in text for token in ("stale", "retry", "skipped", "pending", "permission")):
        return "warning"
    if any(token in text for token in ("completed", "succeeded", "healthy", "resolved")):
        return "ok"
    return "info"


def _notification_item(
    *,
    item_id,
    kind,
    severity,
    title,
    summary,
    created_at=None,
    task_id=None,
    source_key=None,
    source_title=None,
    github_url=None,
    metadata=None,
):
    return {
        "id": item_id,
        "kind": kind,
        "severity": severity,
        "title": title,
        "summary": summary,
        "created_at": created_at,
        "task_id": task_id,
        "source_key": source_key,
        "source_title": source_title,
        "github_url": github_url,
        "metadata": _notification_metadata(metadata),
    }


def _build_notification_center(
    operator_alerts,
    operator_next_steps,
    recent_notifications,
    recent_actions,
    daemon_summary,
    recent_runs,
    recent_tasks,
):
    items = []
    tasks_by_id = {task["task_id"]: task for task in recent_tasks}

    for alert in operator_alerts:
        alert_type = alert.get("alert_type") or "operator_alert"
        count = alert.get("count")
        items.append(
            _notification_item(
                item_id=f"alert:{alert_type}",
                kind="alert",
                severity=alert.get("severity") or "warning",
                title=alert_type,
                summary=f"{count} item(s) need {alert.get('next_action') or 'operator review'}",
                metadata=alert,
            )
        )

    for step in operator_next_steps:
        target_kind = step.get("target_kind") or "target"
        target_id = step.get("target_id") or step.get("event_fingerprint") or "unknown"
        task_id = target_id if target_kind == "task" else step.get("task_id")
        items.append(
            _notification_item(
                item_id=f"next_step:{target_kind}:{target_id}:{step.get('next_action') or 'inspect'}",
                kind="next_step",
                severity=step.get("severity") or "info",
                title=step.get("next_action") or "operator_next_step",
                summary=step.get("reason") or target_id,
                task_id=task_id,
                source_key=step.get("source_key"),
                source_title=step.get("source_title"),
                github_url=step.get("github_url") or step.get("target_url"),
                metadata=step,
            )
        )

    for notification in recent_notifications:
        metadata = notification.get("metadata") or {}
        task_id = _notification_task_id(notification)
        task = tasks_by_id.get(task_id, {})
        summary = (
            (metadata.get("summary") if isinstance(metadata, dict) else None)
            or notification.get("status")
            or notification.get("channel")
            or ""
        )
        items.append(
            _notification_item(
                item_id=f"notification:{notification.get('notification_id')}",
                kind="notification",
                severity=_status_severity(
                    notification.get("notification_type"),
                    notification.get("status"),
                    metadata.get("safe_error") if isinstance(metadata, dict) else None,
                ),
                title=notification.get("notification_type") or "notification",
                summary=summary,
                created_at=notification.get("created_at"),
                task_id=task_id,
                source_key=task.get("source_key"),
                source_title=task.get("source_title"),
                github_url=task.get("github_url"),
                metadata=notification,
            )
        )

    for event in (daemon_summary or {}).get("recent_events") or []:
        metadata = event.get("metadata") or {}
        items.append(
            _notification_item(
                item_id=f"daemon_event:{event.get('daemon_event_id')}",
                kind="daemon_event",
                severity=_status_severity(event.get("status"), event.get("event_type")),
                title=event.get("event_type") or "daemon_event",
                summary=metadata.get("reason") if isinstance(metadata, dict) else event.get("status"),
                created_at=event.get("created_at"),
                metadata=event,
            )
        )

    for action in recent_actions:
        audit_status = action.get("audit_status")
        publish_status = action.get("publish_status")
        metadata = action.get("metadata") or {}
        publish = metadata.get("publish") if isinstance(metadata, dict) else {}
        failed_publish = isinstance(publish, dict) and publish.get("status") == "publish_failed"
        if audit_status not in {"failed", "policy_violation"} and publish_status != "skipped" and not failed_publish:
            continue
        task = tasks_by_id.get(action.get("task_id"), {})
        items.append(
            _notification_item(
                item_id=f"github_action:{action.get('action_id')}",
                kind="github_action",
                severity=_status_severity(
                    audit_status,
                    publish_status,
                    publish.get("status") if isinstance(publish, dict) else None,
                ),
                title=action.get("action_type") or "github_action",
                summary=publish.get("safe_error") if isinstance(publish, dict) else publish_status or audit_status,
                created_at=action.get("created_at"),
                task_id=action.get("task_id"),
                source_key=task.get("source_key"),
                source_title=task.get("source_title"),
                github_url=action.get("target_url") or task.get("github_url"),
                metadata=action,
            )
        )

    for run in recent_runs:
        if run.get("status") not in {"failed", "canceled"}:
            continue
        items.append(
            _notification_item(
                item_id=f"run:{run.get('run_id')}",
                kind="run",
                severity="critical",
                title=f"run {run.get('status')}",
                summary=(run.get("error") or {}).get("status")
                if isinstance(run.get("error"), dict)
                else run.get("status"),
                created_at=run.get("started_at"),
                metadata=run,
            )
        )

    return sorted(
        items,
        key=lambda item: (
            item.get("created_at") or "",
            item["id"],
        ),
        reverse=True,
    )


def _build_work_items(recent_tasks):
    groups = {}
    for task in recent_tasks:
        item_id = task.get("source_key") or task.get("workstream_id") or task.get("task_id")
        if not item_id:
            continue
        group = groups.setdefault(
            item_id,
            {
                "id": item_id,
                "source_key": task.get("source_key"),
                "source_title": task.get("source_title"),
                "source_type": task.get("source_type"),
                "source_number": task.get("source_number"),
                "repo_id": task.get("repo_id"),
                "repo_full_name": task.get("repo_full_name"),
                "github_url": task.get("github_url"),
                "workstream_id": task.get("workstream_id"),
                "latest_task_updated_at": task.get("updated_at"),
                "task_count": 0,
                "operator_state_counts": {},
                "tasks": [],
            },
        )
        if not group.get("source_key") and task.get("source_key"):
            group["source_key"] = task.get("source_key")
        if not group.get("source_title") and task.get("source_title"):
            group["source_title"] = task.get("source_title")
        if not group.get("source_type") and task.get("source_type"):
            group["source_type"] = task.get("source_type")
        if not group.get("source_number") and task.get("source_number"):
            group["source_number"] = task.get("source_number")
        if not group.get("repo_id") and task.get("repo_id"):
            group["repo_id"] = task.get("repo_id")
        if not group.get("repo_full_name") and task.get("repo_full_name"):
            group["repo_full_name"] = task.get("repo_full_name")
        if not group.get("github_url") and task.get("github_url"):
            group["github_url"] = task.get("github_url")
        if task.get("updated_at") and (
            not group.get("latest_task_updated_at") or task["updated_at"] > group["latest_task_updated_at"]
        ):
            group["latest_task_updated_at"] = task["updated_at"]
        group["task_count"] += 1
        state = task.get("operator_state") or "unknown"
        group["operator_state_counts"][state] = group["operator_state_counts"].get(state, 0) + 1
        group["tasks"].append(task)

    state_priority = {
        "needs_attention": 0,
        "worker_stale": 1,
        "worker_retrying": 2,
        "result_pending_audit": 3,
        "waiting_publish": 4,
        "waiting_user": 5,
        "worker_running": 6,
        "waiting_worker": 7,
        "completed": 8,
        "ignored": 9,
    }
    work_items = []
    for group in groups.values():
        group["tasks"] = sorted(
            group["tasks"],
            key=lambda task: (task.get("updated_at") or "", task.get("task_id") or ""),
            reverse=True,
        )
        group["top_operator_state"] = sorted(
            group["operator_state_counts"],
            key=lambda state: (state_priority.get(state, 99), state),
        )[0]
        work_items.append(group)
    return sorted(
        work_items,
        key=lambda group: (group.get("latest_task_updated_at") or "", group["id"]),
        reverse=True,
    )


def _load_priority_pending_events(conn, recent_events, history_limit):
    recent_event_ids = {event["event_id"] for event in recent_events}
    priority_limit = max(history_limit, 20)
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
              ge.event_id,
              ge.event_fingerprint,
              ge.event_type,
              ge.actor_login,
              ge.author_association,
              ge.authorization_status,
              ge.event_at,
              ge.payload_json,
              gs.source_key,
              gs.title AS source_title,
              gs.source_type,
              gs.number,
              gs.html_url,
              NULL AS task_id,
              NULL AS task_relationship,
              NULL AS workstream_id
            FROM github_events ge
            JOIN github_sources gs ON gs.source_id = ge.source_id
            WHERE ge.authorization_status IN (
              'pending_actor_permission',
              'pending_authorization'
            )
            ORDER BY ge.event_at DESC, ge.event_id DESC
            LIMIT ?
            """,
            (priority_limit,),
        )
    ]
    priority_events = []
    for event in rows:
        if event["event_id"] in recent_event_ids:
            continue
        event["payload"] = _decode_json(event.pop("payload_json")) or {}
        priority_events.append(event)
    return priority_events


def _load_repos(conn):
    if not _table_exists(conn, "repos"):
        return []
    return [
        {
            "repo_id": row["repo_id"],
            "full_name": row["full_name"],
        }
        for row in conn.execute(
            """
            SELECT repo_id, full_name
            FROM repos
            ORDER BY full_name, repo_id
            """
        )
    ]


def build_dashboard_data(db_path, history_limit=20):
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        repos = _load_repos(conn)
        recent_tasks = [
            dict(row)
            for row in conn.execute(
                """
                SELECT
                  t.task_id,
                  t.workstream_id,
                  t.lifecycle,
                  t.priority,
                  COALESCE(t.route_id, rd.route_id) AS route_id,
                  COALESCE(t.expected_output, rd.expected_output) AS expected_output,
                  t.updated_at,
                  w.lifecycle AS workstream_lifecycle,
                  w.repo_id,
                  repos.full_name AS repo_full_name,
                  rd.route_decision_id,
                  rd.confidence AS route_confidence,
                  rd.allowed_github_actions_json,
                  rd.recommended_skills_json
                FROM tasks t
                JOIN workstreams w ON w.workstream_id = t.workstream_id
                JOIN repos ON repos.repo_id = w.repo_id
                LEFT JOIN route_decisions rd ON rd.task_id = t.task_id
                ORDER BY t.updated_at DESC, t.task_id DESC
                LIMIT ?
                """,
                (history_limit,),
            )
        ]
        for task in recent_tasks:
            task["allowed_github_actions"] = _decode_json(task.pop("allowed_github_actions_json")) or []
            task["recommended_skills"] = _decode_json(task.pop("recommended_skills_json")) or []
        _attach_task_operator_state(conn, recent_tasks)
        operator_state_counts = dict(sorted(Counter(task["operator_state"] for task in recent_tasks).items()))
        recent_actions = [
            dict(row)
            for row in conn.execute(
                """
                SELECT action_id, result_id, task_id, action_type, target_url, external_id,
                       audit_status, publish_status, created_at, metadata_json
                FROM github_actions
                ORDER BY created_at DESC, action_id DESC
                LIMIT ?
                """,
                (history_limit,),
            )
        ]
        for action in recent_actions:
            action["metadata"] = _decode_json(action.pop("metadata_json"))
        recent_notifications = [
            dict(row)
            for row in conn.execute(
                """
                SELECT notification_id, task_id, notification_type, channel, status,
                       created_at, metadata_json
                FROM notifications
                ORDER BY created_at DESC, notification_id DESC
                LIMIT ?
                """,
                (history_limit,),
            )
        ]
        for notification in recent_notifications:
            notification["metadata"] = _decode_json(notification.pop("metadata_json"))
        recent_worker_results = [
            dict(row)
            for row in conn.execute(
                """
                SELECT result_id, task_id, attempt_id, output_type,
                       consumed_event_fingerprints_json, verification_json,
                       handoff, created_at, metadata_json
                FROM worker_results
                ORDER BY created_at DESC, result_id DESC
                LIMIT ?
                """,
                (history_limit,),
            )
        ]
        used_skill_counts = Counter()
        for result in recent_worker_results:
            result["consumed_event_fingerprints"] = _decode_json(result.pop("consumed_event_fingerprints_json")) or []
            result["verification"] = _decode_json(result.pop("verification_json")) or []
            result["metadata"] = _decode_json(result.pop("metadata_json")) or {}
            used_skill_counts.update(result["metadata"].get("used_skills") or [])
        recent_events = [
            dict(row)
            for row in conn.execute(
                """
                SELECT
                  ge.event_id,
                  ge.event_fingerprint,
                  ge.event_type,
                  ge.actor_login,
                  ge.author_association,
                  ge.authorization_status,
                  ge.event_at,
                  ge.payload_json,
                  gs.source_key,
                  gs.title AS source_title,
                  gs.source_type,
                  gs.number,
                  gs.html_url,
                  te.task_id,
                  te.relationship AS task_relationship,
                  t.workstream_id
                FROM github_events ge
                JOIN github_sources gs ON gs.source_id = ge.source_id
                LEFT JOIN task_events te ON te.event_id = ge.event_id
                LEFT JOIN tasks t ON t.task_id = te.task_id
                ORDER BY ge.event_at DESC, ge.event_id DESC
                LIMIT ?
                """,
                (history_limit,),
            )
        ]
        for event in recent_events:
            event["payload"] = _decode_json(event.pop("payload_json")) or {}
        priority_pending_events = _load_priority_pending_events(
            conn,
            recent_events,
            history_limit,
        )
        event_flow_counts = {
            "authorization_status": dict(
                sorted(Counter(event["authorization_status"] for event in recent_events).items())
            ),
            "task_relationship": dict(
                sorted(Counter(event["task_relationship"] or "unattached" for event in recent_events).items())
            ),
            "authorized_unattached": sum(1 for event in recent_events if _is_authorized_unattached_event(event)),
            "ignored_events": sum(
                1 for event in recent_events if (event["authorization_status"] or "").startswith("ignored_")
            ),
        }
        summary_counts = summarize.summarize_database(db_path)
        daemon_summary = daemon_state.latest_daemon_summary(conn)
        wakeup_summary = wakeup.summarize_wakeups(conn)
        recent_wakeups = wakeup.list_wakeups(conn, limit=history_limit)
        acceptance_metrics = usage.summarize_acceptance_metrics(conn)
        operator_alerts = _build_operator_alerts(
            operator_state_counts,
            event_flow_counts,
            summary_counts,
        )
        operator_next_steps = _build_operator_next_steps(
            recent_tasks,
            recent_events,
            priority_pending_events,
        )
        recent_runs = [
            dict(row)
            for row in conn.execute(
                """
                SELECT run_id, status, started_at, finished_at, config_path, dry_run,
                       summary_json, error_json
                FROM agent_runs
                ORDER BY started_at DESC, run_id DESC
                LIMIT ?
                """,
                (history_limit,),
            )
        ]
        steps_by_run = {run["run_id"]: [] for run in recent_runs}
        if recent_runs:
            placeholders = ",".join("?" for _run in recent_runs)
            for row in conn.execute(
                f"""
                SELECT s.run_id, s.step_key, s.status, s.started_at, s.finished_at,
                       s.output_json, s.error_json,
                       CASE WHEN s.step_key = 'summarize' THEN r.summary_json END AS referenced_summary_json
                FROM run_steps s
                JOIN agent_runs r ON r.run_id = s.run_id
                WHERE s.run_id IN ({placeholders})
                """,
                [run["run_id"] for run in recent_runs],
            ):
                step = dict(row)
                step["output"] = step_output(
                    step["step_key"],
                    _decode_json(step.pop("output_json")),
                    _decode_json(step.pop("referenced_summary_json")),
                )
                step["error"] = _decode_json(step.pop("error_json"))
                steps_by_run[step["run_id"]].append(step)
        repo_steps_by_run = {run["run_id"]: [] for run in recent_runs}
        if recent_runs and _table_exists(conn, "run_repo_steps"):
            placeholders = ",".join("?" for _run in recent_runs)
            for row in conn.execute(
                f"""
                SELECT rrs.run_id, rrs.repo_id, repos.full_name AS repo_full_name,
                       rrs.step_key, rrs.status, rrs.started_at, rrs.finished_at,
                       rrs.output_json, rrs.error_json
                FROM run_repo_steps rrs
                JOIN repos ON repos.repo_id = rrs.repo_id
                WHERE rrs.run_id IN ({placeholders})
                ORDER BY repos.full_name, rrs.step_key
                """,
                [run["run_id"] for run in recent_runs],
            ):
                step = dict(row)
                step["output"] = _decode_json(step.pop("output_json"))
                step["error"] = _decode_json(step.pop("error_json"))
                repo_steps_by_run[step["run_id"]].append(step)
        order = {step: index for index, step in enumerate(RUN_STEP_ORDER)}
        repo_order = {step: index for index, step in enumerate(REPO_STEP_ORDER)}
        for run in recent_runs:
            run["summary"] = _decode_json(run.pop("summary_json"))
            run["error"] = _decode_json(run.pop("error_json"))
            run["steps"] = sorted(
                steps_by_run[run["run_id"]],
                key=lambda step: (
                    order.get(step["step_key"], len(order)),
                    step["step_key"],
                ),
            )
            run["repo_steps"] = sorted(
                repo_steps_by_run.get(run["run_id"], []),
                key=lambda step: (
                    step.get("repo_full_name") or "",
                    repo_order.get(step["step_key"], len(repo_order)),
                    step["step_key"],
                ),
            )
        notification_center = _build_notification_center(
            operator_alerts,
            operator_next_steps,
            recent_notifications,
            recent_actions,
            daemon_summary,
            recent_runs,
            recent_tasks,
        )
        work_items = _build_work_items(recent_tasks)
        return {
            "summary": summary_counts,
            "repos": repos,
            "operator_state_counts": operator_state_counts,
            "recent_tasks": recent_tasks,
            "work_items": work_items,
            "recent_actions": recent_actions,
            "recent_notifications": recent_notifications,
            "notification_center": notification_center,
            "recent_worker_results": recent_worker_results,
            "recent_events": recent_events,
            "event_flow_counts": event_flow_counts,
            "operator_alerts": operator_alerts,
            "operator_next_steps": operator_next_steps,
            "daemon": daemon_summary,
            "wakeup_summary": wakeup_summary,
            "recent_wakeups": recent_wakeups,
            "acceptance_metrics": acceptance_metrics,
            "latest_loop": _load_latest_loop(db_path),
            "knowledge_review": _build_knowledge_review_data(conn),
            "used_skill_counts": dict(sorted(used_skill_counts.items())),
            "recent_runs": recent_runs,
        }


def _build_knowledge_review_data(conn, history_limit=20):
    proposal_repos = _knowledge_proposal_repos(conn)
    if not _table_exists(conn, "knowledge_candidates") or not _table_exists(conn, "runtime_knowledge"):
        return {
            "available": False,
            "submission_boundary": "local_runtime_knowledge_only",
            "counts": {"pending": 0, "approved": 0, "rejected": 0, "active": 0},
            "proposal_repos": proposal_repos,
            "pending_candidates": [],
            "recent_candidates": [],
            "active_knowledge": [],
        }
    candidate_counts = {
        row["status"]: row["count"]
        for row in conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM knowledge_candidates
            GROUP BY status
            """
        )
    }
    active_count = conn.execute("SELECT COUNT(*) FROM runtime_knowledge WHERE active = 1").fetchone()[0]
    pending_candidates = [
        runtime_knowledge.show_candidate(conn, row["candidate_id"])
        for row in conn.execute(
            """
            SELECT candidate_id
            FROM knowledge_candidates
            WHERE status = 'pending'
            ORDER BY created_at DESC, candidate_id
            LIMIT ?
            """,
            (history_limit,),
        )
    ]
    recent_candidates = runtime_knowledge.list_candidates(conn)[:history_limit]
    active_knowledge = [
        {
            "knowledge_id": row["knowledge_id"],
            "candidate_id": row["candidate_id"],
            "repo_id": row["repo_id"],
            "scope_type": row["scope_type"],
            "scope_value": row["scope_value"],
            "title": row["title"],
            "prompt_text": row["prompt_text"],
            "approved_by": row["approved_by"],
            "approved_at": row["approved_at"],
        }
        for row in conn.execute(
            """
            SELECT knowledge_id, candidate_id, repo_id, scope_type, scope_value,
                   title, prompt_text, approved_by, approved_at
            FROM runtime_knowledge
            WHERE active = 1
            ORDER BY approved_at DESC, knowledge_id
            LIMIT ?
            """,
            (history_limit,),
        )
    ]
    return {
        "available": True,
        "submission_boundary": "local_runtime_knowledge_only",
        "counts": {
            "pending": candidate_counts.get("pending", 0),
            "approved": candidate_counts.get("approved", 0),
            "rejected": candidate_counts.get("rejected", 0),
            "active": active_count,
        },
        "proposal_repos": proposal_repos,
        "pending_candidates": [item for item in pending_candidates if item],
        "recent_candidates": recent_candidates,
        "active_knowledge": active_knowledge,
    }


def _knowledge_proposal_repos(conn):
    if not _table_exists(conn, "repos"):
        return []
    memory_counts = {}
    if _table_exists(conn, "project_memory_entries"):
        memory_counts = {
            row["repo_id"]: row["count"]
            for row in conn.execute(
                """
                SELECT repo_id, COUNT(*) AS count
                FROM project_memory_entries
                GROUP BY repo_id
                """
            )
        }
    return [
        {
            "repo_id": row["repo_id"],
            "full_name": row["full_name"],
            "memory_count": memory_counts.get(row["repo_id"], 0),
        }
        for row in conn.execute(
            """
            SELECT repo_id, full_name
            FROM repos
            ORDER BY full_name, repo_id
            """
        )
    ]


def _knowledge_tables_available(conn):
    return all(
        _table_exists(conn, table_name)
        for table_name in [
            "knowledge_candidates",
            "runtime_knowledge",
            "project_memory_entries",
        ]
    )


def load_artifact_preview(db_path, task_id, artifact_type, max_bytes=65536, *, artifact_id=None):
    if not task_id or not artifact_type:
        return None
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT artifact_type, path, bytes
            FROM artifacts
            WHERE task_id = ? AND artifact_type = ?
              AND (? IS NULL OR artifact_id = ?)
            ORDER BY created_at DESC, artifact_id DESC
            LIMIT 1
            """,
            (task_id, artifact_type, artifact_id, artifact_id),
        ).fetchone()
    if not row:
        return None
    registered_path = str(row["path"])
    try:
        root = Path(db_path).expanduser().resolve().parent
        path = Path(registered_path).expanduser().resolve(strict=True)
        relative = path.relative_to(root)
        # Open every component relative to an already-open directory. A worker
        # replacing a checked directory or file with a symlink cannot redirect I/O.
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in relative.parts[:-1]:
                child_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = child_fd
            file_fd = os.open(
                relative.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
        finally:
            os.close(directory_fd)
        with os.fdopen(file_fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode):
                return None
            truncated = info.st_size > max_bytes
            if truncated:
                fh.seek(-max_bytes, 2)
            content = fh.read(max_bytes).decode("utf-8", errors="replace")
    except (OSError, ValueError, RuntimeError):
        return {
            "task_id": task_id,
            "artifact_type": artifact_type,
            "path": registered_path,
            "content": "",
            "truncated": False,
            "missing": True,
        }
    return {
        "task_id": task_id,
        "artifact_type": artifact_type,
        "path": registered_path,
        "bytes": row["bytes"],
        "content": content,
        "truncated": truncated,
        "missing": False,
    }


def render_artifact_preview_text(preview):
    lines = [
        f"task_id={preview['task_id']}",
        f"artifact_type={preview['artifact_type']}",
        f"path={preview['path']}",
        f"truncated={str(bool(preview.get('truncated'))).lower()}",
        "",
    ]
    if preview.get("missing"):
        lines.append("artifact file not found")
    else:
        lines.append(preview.get("content") or "")
    return "\n".join(lines).encode("utf-8")


def knowledge_available(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        return _knowledge_tables_available(conn)
