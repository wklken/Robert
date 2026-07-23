#!/usr/bin/env python3
import argparse
from pathlib import Path
import sqlite3

from robert_agent.common import emit
from robert_agent import web


def _value(value, empty="-"):
    if value is None or value == "":
        return empty
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else empty
    return str(value)


def _field(label, value):
    return f"- {label}: {_value(value)}"


def _find_by_id(items, key, value):
    for item in items:
        if item.get(key) == value:
            return item
    return None


def _format_next_step(step):
    label = f"{step.get('severity', 'info')} {step.get('target_kind')} {step.get('target_id')}"
    parts = [label, step.get("next_action"), step.get("reason")]
    line = " - ".join(_value(part) for part in parts if part)
    if step.get("safe_error"):
        line = f"{line}: {step['safe_error']}"
    if step.get("command"):
        line = f"{line} | command: {step['command']}"
    return f"- {line}"


def _format_task_line(task):
    source = task.get("source_key") or task.get("workstream_id")
    route = task.get("route_id") or task.get("expected_output") or "unrouted"
    return (
        f"- {task['task_id']} [{task.get('operator_state', 'unknown')}] "
        f"{route} {task.get('priority', '')} source={_value(source)}"
    )


def format_status(data):
    summary = data.get("summary") or {}
    state_counts = data.get("operator_state_counts") or {}
    wakeup_summary = data.get("wakeup_summary") or {}
    acceptance_metrics = data.get("acceptance_metrics") or {}
    latest_loop = data.get("latest_loop") or {}
    alerts = data.get("operator_alerts") or []
    steps = data.get("operator_next_steps") or []
    tasks = data.get("recent_tasks") or []
    runs = data.get("recent_runs") or []

    lines = [
        "## Robert status",
        "",
        (
            f"Repos: {summary.get('repos', 0)} | "
            f"Workstreams: {summary.get('workstreams', 0)} | "
            f"Tasks: {summary.get('tasks', 0)} | "
            f"Events: {summary.get('github_events', 0)}"
        ),
        (
            f"Needs attention: {state_counts.get('needs_attention', 0)} | "
            f"Waiting publish: {state_counts.get('waiting_publish', 0)} | "
            f"Worker stale: {state_counts.get('worker_stale', 0)} | "
            f"Waiting worker: {state_counts.get('waiting_worker', 0)}"
        ),
    ]
    if wakeup_summary.get("available"):
        lines.append(
            f"Wakeups pending: {wakeup_summary.get('pending', 0)} | "
            f"Due: {wakeup_summary.get('due', 0)}"
        )
    if acceptance_metrics:
        lines.append(
            f"Results accepted: {acceptance_metrics.get('accepted_results', 0)} | "
            f"Rejected: {acceptance_metrics.get('rejected_results', 0)} | "
            f"Usage: {'available' if acceptance_metrics.get('usage_available') else 'unavailable'}"
        )
    if latest_loop:
        lines.append(
            f"Latest loop: {latest_loop.get('stop_reason', 'unknown')} | "
            f"Cycles: {latest_loop.get('cycles', 0)}"
        )
    daemon = data.get("daemon") or {}
    latest_daemon_run = daemon.get("latest_run") or {}
    latest_daemon_event = daemon.get("latest_event") or {}
    if latest_daemon_run or latest_daemon_event:
        lines.append(
            "Daemon: "
            f"{latest_daemon_run.get('status', 'unknown')} | "
            f"Last event: {latest_daemon_event.get('event_type', 'none')}"
        )

    if alerts:
        lines.extend(["", "### Alerts"])
        for alert in alerts[:8]:
            lines.append(
                "- "
                f"{alert['severity']} {alert['alert_type']}: "
                f"{alert['count']} -> {alert['next_action']}"
            )

    if steps:
        lines.extend(["", "### Next steps"])
        lines.extend(_format_next_step(step) for step in steps[:8])

    if tasks:
        lines.extend(["", "### Recent tasks"])
        lines.extend(_format_task_line(task) for task in tasks[:8])

    if runs:
        latest = runs[0]
        lines.extend(
            [
                "",
                "### Latest run",
                _field("Run", latest.get("run_id")),
                _field("Status", latest.get("status")),
                _field("Started", latest.get("started_at")),
                _field("Finished", latest.get("finished_at")),
            ]
        )

    return "\n".join(lines)


def format_task(data, task_id):
    task = _find_by_id(data.get("recent_tasks") or [], "task_id", task_id)
    if not task:
        return None

    source_number = (
        f"{task.get('source_type') or '-'} #{task.get('source_number') or '-'}"
    )
    lines = [
        f"## Task {task['task_id']}",
        "",
        _field("Operator state", task.get("operator_state")),
        _field("Lifecycle", task.get("lifecycle")),
        _field("Workstream", task.get("workstream_id")),
        _field("Workstream lifecycle", task.get("workstream_lifecycle")),
        _field("Priority", task.get("priority")),
        _field("Route", task.get("route_id") or task.get("expected_output")),
        _field("Route confidence", task.get("route_confidence")),
        _field("Latest attempt", task.get("latest_attempt_status")),
        _field("Heartbeat", task.get("latest_attempt_heartbeat_at")),
        _field("Updated", task.get("updated_at")),
        _field("Source key", task.get("source_key")),
        _field("Source", source_number),
        _field("GitHub", task.get("github_url")),
        _field("Trigger", task.get("trigger_event_fingerprint")),
        _field("Next action", task.get("next_action")),
        _field("Allowed actions", task.get("allowed_github_actions") or []),
        _field("Recommended skills", task.get("recommended_skills") or []),
    ]

    notification = task.get("latest_notification")
    if notification:
        lines.extend(
            [
                "",
                "### Latest notification",
                _field("Type", notification.get("notification_type")),
                _field("Status", notification.get("status")),
                _field("Channel", notification.get("channel")),
                _field("Created", notification.get("created_at")),
            ]
        )

    artifacts = task.get("artifacts") or {}
    if artifacts:
        lines.extend(["", "### Artifacts"])
        for artifact_type, artifact in sorted(artifacts.items()):
            lines.append(
                f"- {artifact_type}: {artifact.get('bytes') or 'unknown'} bytes "
                f"at {artifact.get('path') or '-'}"
            )

    return "\n".join(lines)


def format_run(data, run_id):
    run = _find_by_id(data.get("recent_runs") or [], "run_id", run_id)
    if not run:
        return None

    summary = run.get("summary") or {}
    status = summary.get("overall_status") or run.get("status")
    lines = [
        f"## Run {run['run_id']}",
        "",
        _field("Status", status),
        _field("Started", run.get("started_at")),
        _field("Finished", run.get("finished_at")),
        _field("Config", run.get("config_path")),
        _field("Dry run", bool(run.get("dry_run"))),
    ]
    if run.get("error"):
        lines.append(_field("Error", run.get("error")))
    repo_summaries = summary.get("repo_summaries") or []
    if repo_summaries:
        lines.extend(["", "Repos:"])
        for repo in repo_summaries[:8]:
            lines.append(
                f"- {repo.get('full_name') or repo.get('repo_id')}: {repo.get('status')}"
            )
    steps = run.get("steps") or []
    if steps:
        lines.extend(["", "### Steps"])
        for step in steps:
            line = f"- {step['step_key']}: {step['status']}"
            if step.get("error"):
                line = f"{line} error={step['error']}"
            lines.append(line)
    return "\n".join(lines)


def format_artifact(preview):
    if not preview:
        return None
    if preview.get("missing"):
        return None
    content = preview.get("content") or ""
    fence = "```"
    if fence in content:
        fence = "````"
    lines = [
        f"## Artifact {preview['artifact_type']} for {preview['task_id']}",
        "",
        _field("Path", preview.get("path")),
        _field("Bytes", preview.get("bytes")),
        _field("Truncated", bool(preview.get("truncated"))),
        "",
        f"{fence}text",
        content,
        fence,
    ]
    return "\n".join(lines)


def _success(command, message):
    return {
        "ok": True,
        "status": "ready",
        "command": command,
        "format": "markdown",
        "message": message,
    }


def _not_found(safe_error):
    return {"ok": False, "status": "not_found", "safe_error": safe_error}


def _load_data(db_path, history_limit):
    path = Path(db_path).expanduser()
    if not path.is_file():
        return None, {
            "ok": False,
            "status": "missing_database",
            "safe_error": f"database not found: {path}",
        }
    try:
        return web.build_dashboard_data(path, history_limit=history_limit), None
    except sqlite3.Error as exc:
        return None, {"ok": False, "status": "database_error", "safe_error": str(exc)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--history-limit", type=int, default=20)
    parser.add_argument("--max-bytes", type=int, default=8192)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status")

    task_parser = subparsers.add_parser("task")
    task_parser.add_argument("task_id")

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("run_id")

    artifact_parser = subparsers.add_parser("artifact")
    artifact_parser.add_argument("task_id")
    artifact_parser.add_argument("artifact_type")

    args = parser.parse_args(argv)
    data, error = _load_data(args.db, args.history_limit)
    if error:
        return emit(error, exit_code=3)

    if args.command == "status":
        return emit(_success("status", format_status(data)))

    if args.command == "task":
        message = format_task(data, args.task_id)
        if not message:
            safe_error = f"task not found in dashboard history: {args.task_id}"
            return emit(_not_found(safe_error), exit_code=3)
        return emit(_success("task", message))

    if args.command == "run":
        message = format_run(data, args.run_id)
        if not message:
            safe_error = f"run not found in dashboard history: {args.run_id}"
            return emit(_not_found(safe_error), exit_code=3)
        return emit(_success("run", message))

    if args.command == "artifact":
        preview = web.load_artifact_preview(
            Path(args.db).expanduser(),
            args.task_id,
            args.artifact_type,
            max_bytes=args.max_bytes,
        )
        message = format_artifact(preview)
        if not message:
            safe_error = f"artifact not found: {args.task_id} {args.artifact_type}"
            return emit(_not_found(safe_error), exit_code=3)
        return emit(_success("artifact", message))

    return emit(
        {"ok": False, "status": "unsupported", "safe_error": args.command},
        exit_code=3,
    )


if __name__ == "__main__":
    raise SystemExit(main())
