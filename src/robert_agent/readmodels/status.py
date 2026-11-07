#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from robert_agent.foundation.commands import emit
from robert_agent.persistence import database as db_database
from robert_agent.persistence import errors as db_errors
from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import agent_runs as db_read_agent_runs
from robert_agent.persistence.queries import attempts as db_read_attempts
from robert_agent.persistence.queries import github_actions as db_read_github_actions
from robert_agent.persistence.queries import github_events as db_read_github_events
from robert_agent.persistence.queries import github_sources as db_read_github_sources
from robert_agent.persistence.queries import notifications as db_read_notifications
from robert_agent.persistence.queries import sqlite_master as db_read_sqlite_master
from robert_agent.persistence.queries import task_events as db_read_task_events
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import worker_phases as db_read_worker_phases
from robert_agent.persistence.queries import worker_results as db_read_worker_results
from robert_agent.persistence.queries import (
    workstream_sources as db_read_workstream_sources,
)
from robert_agent.persistence.queries import workstreams as db_read_workstreams
from robert_agent.persistence.queries.readviews import run_history, task_history
from robert_agent.persistence.queries.readviews import sources as source_history
from robert_agent.readmodels import dashboard as web
from robert_agent.readmodels import execution_metrics
from robert_agent.readmodels.history import step_output

DEFAULT_DB = "~/.local/share/robert/robert.sqlite3"

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


def _decode_json(value, default=None):
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _row_dict(row):
    return dict(row) if row else None


def _checked_at_utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _short_text(value, max_chars=240):
    if value is None:
        return None
    text = str(value)
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}...[truncated {len(text) - max_chars} chars]"


def _compact_json(value, max_items=8, max_chars=240):
    if isinstance(value, dict):
        items = list(value.items())
        compact = {
            key: _compact_json(item, max_items=max_items, max_chars=max_chars) for key, item in items[:max_items]
        }
        if len(items) > max_items:
            compact["_truncated_keys"] = len(items) - max_items
        return compact
    if isinstance(value, list):
        compact = [_compact_json(item, max_items=max_items, max_chars=max_chars) for item in value[:max_items]]
        if len(value) > max_items:
            compact.append({"_truncated_items": len(value) - max_items})
        return compact
    if isinstance(value, str):
        return _short_text(value, max_chars=max_chars)
    return value


def _compact_command(command):
    if isinstance(command, list):
        head = []
        for item in command[:16]:
            head.append(_short_text(item, max_chars=120))
            if item == "--":
                break
        return {
            "argv0": command[0] if command else None,
            "argc": len(command),
            "head": head,
            "truncated_after_separator": "--" in head,
        }
    if isinstance(command, str):
        return {"text": _short_text(command)}
    return None


def _compact_dispatch(dispatch):
    if not isinstance(dispatch, dict):
        return {}
    compact = {
        key: dispatch.get(key)
        for key in ["ok", "status", "task_id", "attempt_id", "pid", "returncode", "safe_error"]
        if key in dispatch
    }
    if "command" in dispatch:
        compact["command"] = _compact_command(dispatch.get("command"))
    for key in ["stdout_path", "stderr_path"]:
        if key in dispatch:
            compact[key] = dispatch.get(key)
    return compact


def _proc_process_status(pid_value):
    stat_path = Path(f"/proc/{pid_value}/stat")
    try:
        stat_text = stat_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"status": "not_found", "pid": pid_value}
    except OSError as exc:
        return {"status": "unknown", "pid": pid_value, "safe_error": str(exc)}
    parts = stat_text.rsplit(") ", 1)
    if len(parts) != 2:
        return {"status": "unknown", "pid": pid_value, "safe_error": "invalid /proc stat"}
    fields = parts[1].split()
    state = fields[0] if fields else ""
    return {
        "status": "zombie" if state in {"Z", "X", "x"} else "running",
        "pid": pid_value,
        "stat": state,
        "source": "proc",
    }


def _process_status(pid):
    if pid in (None, "", 0):
        return {"status": "unknown", "pid": pid}
    try:
        pid_value = int(pid)
    except (TypeError, ValueError):
        return {"status": "invalid_pid", "pid": pid}
    proc_status = _proc_process_status(pid_value)
    if proc_status["status"] in {"running", "zombie"}:
        return proc_status
    alive_by_signal = False
    try:
        os.kill(pid_value, 0)
        alive_by_signal = True
    except ProcessLookupError:
        return {"status": "not_found", "pid": pid_value}
    except PermissionError:
        return {"status": "running", "pid": pid_value, "visibility": "permission_denied"}
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid_value), "-o", "pid=", "-o", "etime=", "-o", "time=", "-o", "stat="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if alive_by_signal:
            return {"status": "running", "pid": pid_value, "source": "signal", "safe_error": str(exc)}
        return {"status": "unknown", "pid": pid_value, "safe_error": str(exc)}
    output = result.stdout.strip()
    if result.returncode != 0 or not output:
        if alive_by_signal:
            return {"status": "running", "pid": pid_value, "source": "signal"}
        return {"status": "not_found", "pid": pid_value}
    parts = output.split(None, 3)
    stat = parts[3] if len(parts) > 3 else ""
    return {
        "status": "zombie" if "Z" in stat else "running",
        "pid": pid_value,
        "etime": parts[1] if len(parts) > 1 else None,
        "cpu_time": parts[2] if len(parts) > 2 else None,
        "stat": stat,
    }


def _connect(db_path):
    path = Path(db_path).expanduser()
    if not path.is_file():
        return None, {
            "ok": False,
            "status": "missing_database",
            "safe_error": f"database not found: {path}",
        }
    conn = db_database.connect(path, foreign_keys=True, busy_timeout_ms=5000)
    return conn, None


def _compact_task(task):
    return {
        "task_id": task.get("task_id"),
        "operator_state": task.get("operator_state"),
        "lifecycle": task.get("lifecycle"),
        "workstream_id": task.get("workstream_id"),
        "route_id": task.get("route_id") or task.get("expected_output"),
        "latest_attempt_id": task.get("latest_attempt_id"),
        "latest_attempt_status": task.get("latest_attempt_status"),
        "next_action": task.get("next_action"),
        "updated_at": task.get("updated_at"),
        "source_key": task.get("source_key"),
    }


def build_status(db_path, history_limit=5):
    db_path = Path(db_path).expanduser()
    data = web.build_dashboard_data(Path(db_path).expanduser(), history_limit=history_limit)
    with closing(db_database.connect(db_path)) as conn:
        timings = execution_metrics.summarize(conn)
        from robert_agent.supervision import store as supervision_store

        supervision = (
            supervision_store.list_incidents(conn, status="active", limit=history_limit)
            if supervision_store.has_schema(conn)
            else []
        )
    latest_run = (data.get("recent_runs") or [{}])[0]
    return {
        "summary": data.get("summary") or {},
        "operator_state_counts": data.get("operator_state_counts") or {},
        "daemon": data.get("daemon") or {},
        "wakeup_summary": data.get("wakeup_summary") or {},
        "acceptance_metrics": data.get("acceptance_metrics") or {},
        "execution_timings": timings,
        "supervision_incidents": supervision,
        "latest_loop": data.get("latest_loop") or {},
        "operator_alerts": (data.get("operator_alerts") or [])[:history_limit],
        "operator_next_steps": (data.get("operator_next_steps") or [])[:history_limit],
        "recent_tasks": [_compact_task(task) for task in (data.get("recent_tasks") or [])[:history_limit]],
        "latest_run": {
            "run_id": latest_run.get("run_id"),
            "status": latest_run.get("status"),
            "started_at": latest_run.get("started_at"),
            "finished_at": latest_run.get("finished_at"),
        },
    }


def _actions_for_task(conn, task_id):
    return [_row_dict(row) for row in db_read_github_actions.find_for_actions_for_task(conn, task_id)]


def _artifacts_for(conn, task_id=None, attempt_id=None):
    rows = task_history.artifacts_for_tasks(conn, (task_id,) if task_id else None, attempt_id=attempt_id)
    fields = ("artifact_type", "path", "bytes", "created_at")
    return [{key: row[key] for key in fields} for row in rows]


def _latest_attempt_for_task(conn, task_id):
    attempt = _row_dict(db_records.first(db_read_attempts.find_for_latest_attempt_for_task(conn, task_id)))
    if not attempt:
        return None
    metadata = _decode_json(attempt.pop("metadata_json"), {}) or {}
    dispatch = _compact_dispatch(metadata.get("dispatch"))
    attempt["failure"] = _decode_json(attempt.pop("failure_json"), {})
    attempt["dispatch"] = dispatch
    attempt["process"] = _process_status(dispatch.get("pid"))
    return attempt


def _recent_phases_for_attempt(conn, attempt_id, limit=5):
    return [_row_dict(row) for row in db_read_worker_phases.find_for_latest_worker_phases(conn, attempt_id, limit)]


def _results_for_task(conn, task_id, limit=5):
    results = []
    for row in db_read_worker_results.find_for_results_for_task(conn, task_id, limit):
        result = _row_dict(row)
        verification = _decode_json(result.pop("verification_json"), []) or []
        metadata = _decode_json(result.pop("metadata_json"), {}) or {}
        result["verification_count"] = len(verification)
        result["handoff"] = _short_text(result.get("handoff"), max_chars=500)
        result["used_skills"] = (metadata.get("used_skills") or [])[:8]
        results.append(result)
    return results


def _action_counts_for_task(conn, task_id):
    rows = [_row_dict(row) for row in db_read_github_actions.find_for_action_counts_for_task(conn, task_id)]
    audit_counts = Counter()
    publish_counts = Counter()
    total = 0
    for row in rows:
        count = row["count"]
        total += count
        audit_counts[row["audit_status"]] += count
        publish_counts[row["publish_status"]] += count
    return {
        "total": total,
        "audit_status": dict(sorted(audit_counts.items())),
        "publish_status": dict(sorted(publish_counts.items())),
    }


def _latest_notification_for_task(conn, task_id):
    notification = _row_dict(
        db_records.first(db_read_notifications.find_for_latest_notification_for_task(conn, task_id))
    )
    if not notification:
        return None
    notification["metadata"] = _compact_json(_decode_json(notification.pop("metadata_json"), {}) or {})
    return notification


def build_task(conn, task_id, result_limit=5):
    task = _row_dict(db_records.first(db_read_tasks.find_for_build_task(conn, task_id)))
    if not task:
        return None
    task["allowed_github_actions"] = _decode_json(task.pop("allowed_github_actions_json"), [])
    task["recommended_skills"] = _decode_json(task.pop("recommended_skills_json"), [])
    task["latest_attempt"] = _latest_attempt_for_task(conn, task_id)
    if task["latest_attempt"]:
        task["recent_phases"] = _recent_phases_for_attempt(
            conn,
            task["latest_attempt"]["attempt_id"],
            limit=5,
        )
        task["latest_phase"] = task["recent_phases"][0] if task["recent_phases"] else None
    else:
        task["recent_phases"] = []
        task["latest_phase"] = None
    task["results"] = _results_for_task(conn, task_id, limit=result_limit)
    task["latest_result"] = task["results"][0] if task["results"] else None
    task["action_counts"] = _action_counts_for_task(conn, task_id)
    task["latest_notification"] = _latest_notification_for_task(conn, task_id)
    task["actions"] = _actions_for_task(conn, task_id)
    task["artifacts"] = _artifacts_for(conn, task_id=task_id)
    if db_read_sqlite_master.table_exists(conn, "review_sessions"):
        from robert_agent.persistence.rounds import review as review_store

        task["review"] = review_store.task_status(conn, task_id)
    from robert_agent.persistence.rounds import vote as vote_store

    task["vote"] = vote_store.task_status(conn, task_id)
    task["events"] = [_row_dict(row) for row in db_read_task_events.find_for_build_task(conn, task_id)]
    return task


def _latest_run_id(conn):
    row = db_records.first(db_read_agent_runs.find_for_latest_run_id(conn))
    return row["run_id"] if row else None


def _compact_run(row, step_counts=None):
    run = _row_dict(row)
    if not run:
        return None
    run["dry_run"] = bool(run["dry_run"])
    run["summary"] = _compact_json(_decode_json(run.pop("summary_json"), {}) or {})
    run["error"] = _compact_json(_decode_json(run.pop("error_json"), {}) or {})
    run["step_status_counts"] = dict(sorted((step_counts or {}).items()))
    return run


def build_runs(conn, limit=5):
    runs = [_row_dict(row) for row in db_read_agent_runs.find_for_build_dashboard_data(conn, limit)]
    if not runs:
        return []
    counts = {run["run_id"]: Counter() for run in runs}
    for row in run_history.step_status_counts(conn, [run["run_id"] for run in runs]):
        counts[row["run_id"]][row["status"]] += row["count"]
    return [_compact_run(run, counts[run["run_id"]]) for run in runs]


def build_run(conn, run_id):
    if run_id == "latest":
        run_id = _latest_run_id(conn)
        if not run_id:
            return None
    run = _row_dict(db_records.first(db_read_agent_runs.find_for_build_run(conn, run_id)))
    if not run:
        return None
    run["dry_run"] = bool(run["dry_run"])
    summary = _decode_json(run.pop("summary_json"), {}) or {}
    run["summary"] = _compact_json(summary)
    run["error"] = _compact_json(_decode_json(run.pop("error_json"), {}) or {})
    run["steps"] = [
        {
            **_row_dict(row),
            "output": _compact_json(
                step_output(
                    row["step_key"],
                    _decode_json(row["output_json"], {}) or {},
                    _decode_json(row["referenced_summary_json"], {}) or {},
                )
            ),
            "error": _compact_json(_decode_json(row["error_json"], {}) or {}),
        }
        for row in run_history.steps(conn, (run_id,))
    ]
    step_order = {step: index for index, step in enumerate(web.RUN_STEP_ORDER)}
    run["steps"].sort(key=lambda step: (step_order.get(step["step_key"], len(step_order)), step["step_key"]))
    for step in run["steps"]:
        step.pop("run_id", None)
        step.pop("output_json", None)
        step.pop("error_json", None)
        step.pop("referenced_summary_json", None)
    if db_read_sqlite_master.table_exists(conn, "run_repo_steps"):
        repo_steps = [dict(row) for row in run_history.repo_steps(conn, (run_id,))]
        repo_order = {step: index for index, step in enumerate(REPO_STEP_ORDER)}
        for step in repo_steps:
            step["output"] = _compact_json(_decode_json(step.pop("output_json"), {}) or {})
            step["error"] = _compact_json(_decode_json(step.pop("error_json"), {}) or {})
        run["repo_steps"] = sorted(
            repo_steps,
            key=lambda step: (
                step.get("repo_full_name") or "",
                repo_order.get(step["step_key"], len(repo_order)),
                step["step_key"],
            ),
        )
    else:
        run["repo_steps"] = []
    return run


def build_attempt(conn, attempt_id):
    attempt = _row_dict(db_records.first(db_read_attempts.find_for_build_attempt(conn, attempt_id)))
    if not attempt:
        return None
    metadata = _decode_json(attempt.pop("metadata_json"), {}) or {}
    dispatch = _compact_dispatch(metadata.get("dispatch"))
    attempt["failure"] = _decode_json(attempt.pop("failure_json"), {})
    attempt["dispatch"] = dispatch
    attempt["process"] = _process_status(dispatch.get("pid"))
    attempt["phases"] = [_row_dict(row) for row in db_read_worker_phases.find_for_build_attempt(conn, attempt_id)]
    attempt["result"] = _row_dict(
        db_records.first(
            db_read_worker_results.find_result_id_and_output_type_and_created_at_and_handoff_by_attempt_id(
                conn, attempt_id
            )
        )
    )
    attempt["artifacts"] = _artifacts_for(conn, attempt_id=attempt_id)
    return attempt


def _payload_summary(payload):
    if not isinstance(payload, dict):
        return {"value": _compact_json(payload)}
    summary = {"keys": sorted(payload.keys())[:20]}
    for key in ["id", "node_id", "url", "html_url", "intent", "state", "action"]:
        if key in payload:
            summary[key] = _compact_json(payload.get(key))
    for key in ["title", "body"]:
        if key in payload and payload[key] is not None:
            text = str(payload[key])
            summary[f"{key}_chars"] = len(text)
            summary[f"{key}_preview"] = _short_text(text, max_chars=180)
    return summary


def _event_candidates(identifier):
    candidates = [identifier]
    if ":" not in identifier:
        for prefix in ["comment", "review_comment", "review", "assigned", "notification"]:
            candidates.append(f"{prefix}:{identifier}")
    return list(dict.fromkeys(candidates))


def build_event(conn, identifier):
    event = None
    for candidate in _event_candidates(identifier):
        event = _row_dict(db_records.first(db_read_github_events.find_for_build_event(conn, candidate)))
        if event:
            break
    if not event:
        return None
    event_id = event["event_id"]
    payload = _decode_json(event.pop("payload_json"), {}) or {}
    event["payload_summary"] = _payload_summary(payload)
    event["tasks"] = [_row_dict(row) for row in db_read_task_events.find_for_build_event(conn, event_id)]
    return event


def build_events(conn, query, limit=10):
    like = f"%{query}%"
    events = []
    for row in db_read_github_events.find_for_build_events(conn, query, like, like, like, like, limit):
        event = _row_dict(row)
        payload = _decode_json(event.pop("payload_json"), {}) or {}
        event["payload_summary"] = _payload_summary(payload)
        events.append(event)
    return events


def build_artifact(db_path, task_id, artifact_type, max_bytes=65536):
    return web.load_artifact_preview(
        Path(db_path).expanduser(),
        task_id,
        artifact_type,
        max_bytes=max_bytes,
    )


def _source_matches(conn, identifier):
    like = f"%{identifier}%"
    return [
        _row_dict(row)
        for row in db_read_github_sources.find_for_source_matches(conn, identifier, identifier, identifier, like, like)
    ]


def build_source(conn, identifier, limit=50):
    sources = _source_matches(conn, identifier)
    if not sources:
        return None
    for source in sources:
        source["metadata"] = _compact_json(_decode_json(source.pop("metadata_json"), {}) or {})
    source_ids = [source["source_id"] for source in sources]
    workstreams = [dict(row) for row in source_history.linked_workstreams(conn, source_ids)]
    workstream_ids = [stream["workstream_id"] for stream in workstreams]
    tasks = (
        [dict(row) for row in source_history.tasks_with_latest_attempt(conn, workstream_ids, limit)]
        if workstream_ids
        else []
    )
    events = [dict(row) for row in source_history.recent_events(conn, source_ids, min(limit, 50))]
    return {
        "sources": sources,
        "workstreams": workstreams,
        "tasks": tasks,
        "events": events,
    }


def build_workstream(conn, workstream_id):
    stream = _row_dict(db_records.first(db_read_workstreams.find_for_build_workstream(conn, workstream_id)))
    if not stream:
        return None
    stream["tasks"] = [_row_dict(row) for row in db_read_tasks.find_for_build_workstream(conn, workstream_id)]
    stream["sources"] = [
        _row_dict(row) for row in db_read_workstream_sources.find_for_build_workstream(conn, workstream_id)
    ]
    return stream


def _ready(command, payload):
    return {
        "ok": True,
        "status": "ready",
        "command": command,
        "format": "json",
        "checked_at_utc": _checked_at_utc(),
        "data": payload,
    }


def _not_found(kind, identifier):
    return {
        "ok": False,
        "status": "not_found",
        "safe_error": f"{kind} not found: {identifier}",
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--history-limit", type=int, default=5)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status")

    runs_parser = subparsers.add_parser("runs")
    runs_parser.add_argument("--limit", type=int)

    task_parser = subparsers.add_parser("task")
    task_parser.add_argument("task_id")
    task_parser.add_argument("--limit", type=int, default=5)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("run_id")

    attempt_parser = subparsers.add_parser("attempt")
    attempt_parser.add_argument("attempt_id")

    workstream_parser = subparsers.add_parser("workstream")
    workstream_parser.add_argument("workstream_id")

    event_parser = subparsers.add_parser("event")
    event_parser.add_argument("event_fingerprint")

    events_parser = subparsers.add_parser("events")
    events_parser.add_argument("query")
    events_parser.add_argument("--limit", type=int, default=10)

    source_parser = subparsers.add_parser("source")
    source_parser.add_argument("identifier")
    source_parser.add_argument("--limit", type=int, default=50)

    artifact_parser = subparsers.add_parser("artifact")
    artifact_parser.add_argument("task_id")
    artifact_parser.add_argument("artifact_type")
    artifact_parser.add_argument("--max-bytes", type=int, default=65536)

    args = parser.parse_args(argv)
    conn, error = _connect(args.db)
    if error:
        return emit(error, exit_code=3)
    with closing(conn):
        try:
            if args.command == "status":
                return emit(_ready("status", build_status(args.db, args.history_limit)))
            if args.command == "runs":
                return emit(_ready("runs", build_runs(conn, args.limit or args.history_limit)))
            if args.command == "task":
                payload = build_task(conn, args.task_id, result_limit=args.limit)
                if not payload:
                    return emit(_not_found("task", args.task_id), exit_code=3)
                return emit(_ready("task", payload))
            if args.command == "run":
                payload = build_run(conn, args.run_id)
                if not payload:
                    return emit(_not_found("run", args.run_id), exit_code=3)
                return emit(_ready("run", payload))
            if args.command == "attempt":
                payload = build_attempt(conn, args.attempt_id)
                if not payload:
                    return emit(_not_found("attempt", args.attempt_id), exit_code=3)
                return emit(_ready("attempt", payload))
            if args.command == "workstream":
                payload = build_workstream(conn, args.workstream_id)
                if not payload:
                    return emit(_not_found("workstream", args.workstream_id), exit_code=3)
                return emit(_ready("workstream", payload))
            if args.command == "event":
                payload = build_event(conn, args.event_fingerprint)
                if not payload:
                    return emit(_not_found("event", args.event_fingerprint), exit_code=3)
                return emit(_ready("event", payload))
            if args.command == "events":
                return emit(_ready("events", build_events(conn, args.query, limit=args.limit)))
            if args.command == "source":
                payload = build_source(conn, args.identifier, limit=args.limit)
                if not payload:
                    return emit(_not_found("source", args.identifier), exit_code=3)
                return emit(_ready("source", payload))
            if args.command == "artifact":
                payload = build_artifact(
                    args.db,
                    args.task_id,
                    args.artifact_type,
                    max_bytes=args.max_bytes,
                )
                if not payload:
                    return emit(
                        _not_found("artifact", f"{args.task_id}:{args.artifact_type}"),
                        exit_code=3,
                    )
                return emit(_ready("artifact", payload))
        except db_errors.Error as exc:
            return emit(
                {"ok": False, "status": "database_error", "safe_error": str(exc)},
                exit_code=3,
            )

    return emit({"ok": False, "status": "unsupported", "safe_error": args.command}, exit_code=3)


if __name__ == "__main__":
    raise SystemExit(main())
