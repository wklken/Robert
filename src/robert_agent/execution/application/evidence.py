"""Collect attempt and command evidence for recovery."""

from robert_agent.foundation.contracts.execution import RoundStatus

from robert_agent.persistence.dialects import sqlite as db_sqlite

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import artifacts as db_read_artifacts
from robert_agent.persistence.queries import attempts as db_read_attempts
from robert_agent.persistence.queries import worker_phases as db_read_worker_phases
from robert_agent.persistence.stores import worker_phases as db_write_worker_phases
from robert_agent.supervision import store as supervision_store
from pathlib import Path
from robert_agent.adapters.processes import attempt as attempt_process
from robert_agent.execution import retry as retry_policy
from robert_agent.foundation import commands as control_commands
from robert_agent.foundation import values
from robert_agent.execution import command as command_heartbeat
from robert_agent.execution import progress as worker_snapshot
from robert_agent.execution import rounds as native_rounds
import hashlib
import json
import os
import subprocess


def tail_file(path, max_lines=80, max_bytes=32768):
    if not path:
        return ""
    try:
        path_obj = Path(path)
        size = path_obj.stat().st_size
        with path_obj.open("rb") as handle:
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def run_inspection_command(command, cwd):
    try:
        completed = control_commands.run_command(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        raise  # An incomplete observation cannot establish absence of progress.
    except (subprocess.SubprocessError, OSError) as exc:
        return {
            "ok": False,
            "status": "failed",
            "command": command,
            "safe_error": str(exc),
            "error_type": type(exc).__name__,
        }
    return {
        "ok": completed.returncode == 0,
        "status": "completed",
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip()[:8000],
        "stderr": completed.stderr.strip()[:2000],
    }


def git_recovery_summary(worktree_path):
    if not worktree_path or not Path(worktree_path).exists():
        missing = {
            "ok": False,
            "status": "missing_worktree",
            "command": [],
            "stdout": "",
            "stderr": "",
        }
        return {
            "ok": False,
            "status": "missing_worktree",
            "worktree_path": str(worktree_path or ""),
            "git_status_short": missing,
            "git_diff_stat": missing,
            "git_branch": missing,
        }
    status = run_inspection_command(["git", "status", "--short"], worktree_path)
    diff_stat = run_inspection_command(["git", "diff", "--stat"], worktree_path)
    branch = run_inspection_command(["git", "branch", "--show-current"], worktree_path)
    return {
        "ok": status.get("ok", False),
        "worktree_path": str(worktree_path),
        "git_status_short": status,
        "git_diff_stat": diff_stat,
        "git_branch": branch,
    }


def _attempt_log_tails(conn, attempt_id):
    rows = db_read_artifacts.find_for_attempt_log_tails(conn, attempt_id)
    tails = {}
    for artifact_type, path in rows:
        if artifact_type in tails:
            continue
        tails[artifact_type] = {
            "path": path,
            "tail": tail_file(path),
        }
    return tails


def _latest_worker_phases(conn, attempt_id, limit=8):
    rows = db_read_worker_phases.find_for_latest_worker_phases(conn, attempt_id, limit)
    return [
        {
            "phase": phase,
            "status": status,
            "summary": summary,
            "next_step": next_step or "",
            "created_at": created_at,
        }
        for phase, status, summary, next_step, created_at in rows
    ]


def _command_process_status(pid, identity):
    try:
        pid = int(pid)
        if pid <= 0:
            return "not_found"
        os.kill(pid, 0)
    except (TypeError, ValueError, ProcessLookupError):
        return "not_found"
    except PermissionError:
        return "unknown"
    if attempt_process.process_state(pid) in {"Z", "X", "x"}:
        return "not_found"
    current_identity = command_heartbeat.process_identity(pid)
    if identity and current_identity and identity != current_identity:
        return "not_found"
    return "alive" if identity and current_identity else "unknown"


def attempt_command_records(conn, attempt_id, task_id, *, persist=True):
    directories = set()
    for (path,) in db_read_artifacts.find_path_by_attempt_id(conn, attempt_id):
        directories.add(Path(path).parent.resolve())
    for _seq, name, path in db_sqlite.database_list(conn):
        if name == "main" and path:
            directories.add((Path(path).parent / "artifacts" / task_id).resolve())
    commands = []
    seen_paths = set()
    for directory in sorted(directories):
        for path in sorted(directory.glob(f"{command_heartbeat.safe_filename_part(attempt_id)}.*.command.json")):
            try:
                path = path.resolve()
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                if path.stat().st_size > 65536:
                    continue
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (
                not isinstance(record, dict)
                or record.get("task_id") != task_id
                or record.get("attempt_id") != attempt_id
            ):
                continue
            if record.get("phase") not in worker_snapshot.VALID_PHASES:
                continue
            record = {**record, "command_record_path": str(path)}
            terminal = record.get("status") in {"completed", "failed"}
            if terminal:
                record["process_status"] = "finished"
            else:
                statuses = [
                    _command_process_status(record.get("pid"), record.get("process_identity")),
                    _command_process_status(record.get("wrapper_pid"), record.get("wrapper_identity")),
                ]
                record["process_status"] = (
                    "alive" if "alive" in statuses else "unknown" if "unknown" in statuses else "not_found"
                )
            commands.append(record)
    if persist:
        _backfill_command_phases(conn, attempt_id, commands)
    return commands


def _backfill_command_phases(conn, attempt_id, commands):
    for record in commands:
        finished_at = record.get("finished_at")
        if (
            record.get("status") not in {"completed", "failed"}
            or not finished_at
            or record.get("snapshot_status") != "deferred"
        ):
            continue
        try:
            values.parse_time(finished_at)
        except (ValueError, TypeError, AttributeError):
            continue
        summary = record.get("summary") or f"Command exited {record.get('returncode')}"
        if db_records.first(
            db_read_worker_phases.find_for_backfill_command_phases(
                conn, attempt_id, record["phase"], record["status"], summary, finished_at
            )
        ):
            continue
        phase_id = "phase-command-" + hashlib.sha256(record["command_record_path"].encode()).hexdigest()[:24]
        db_write_worker_phases.insert_for_backfill_command_phases(
            conn,
            phase_id,
            attempt_id,
            record["phase"],
            record["status"],
            summary,
            "Inspect saved command output and record result",
            finished_at,
        )


def terminal_transport_failure(logs):
    for payload in logs.values():
        for line in reversed(payload.get("tail", "").splitlines()):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "turn.completed":
                break
            if event.get("type") not in {"error", "turn.failed"}:
                continue
            error = event.get("error") or event.get("message") or ""
            message = error.get("message", "") if isinstance(error, dict) else str(error)
            if any(
                marker in message.lower()
                for marker in (
                    "stream disconnected",
                    "stream closed before response.completed",
                    "reconnecting failed",
                    "connection reset",
                    "transport error",
                )
            ):
                return {"type": event["type"], "message": message}
            if event["type"] == "turn.failed":
                break
    return None


def inspect_attempt_recovery(conn, attempt_id, task_id, worktree_path):

    review = native_rounds.session(conn, task_id=task_id) if conn is not None else None
    if review:
        row = db_records.first(db_read_attempts.find_metadata_json_by_attempt_id(conn, attempt_id))
        diagnostic = values.json_object(row[0] if row else None).get("submission_validation", {})
        return {
            "status": "recoverable_progress"
            if review["status"] not in {RoundStatus.CANCELED, RoundStatus.SUPERSEDED, RoundStatus.ATTENTION}
            else "unrecoverable",
            "reason_code": "native_review_checkpoint",
            "round_id": review["round_id"],
            "task_id": task_id,
            "previous_attempt_id": attempt_id,
            "worktree_path": str(worktree_path or review["manifest"].get("worktree_path", "")),
            "progress_signals": ["native_review_checkpoint"],
            "logs": _attempt_log_tails(conn, attempt_id),
            "commands": [],
            **(
                {
                    key: diagnostic[key]
                    for key in ("reason_code", "field_path", "findings", "safe_error", "required_action")
                    if key in diagnostic
                }
                if review["status"] == RoundStatus.ATTENTION
                else {}
            ),
        }
    commands = attempt_command_records(conn, attempt_id, task_id, persist=False)
    logs = _attempt_log_tails(conn, attempt_id)
    git_summary = (
        {} if conn is not None and supervision_store.repair_task(conn, task_id) else git_recovery_summary(worktree_path)
    )
    _backfill_command_phases(conn, attempt_id, commands)
    phases = _latest_worker_phases(conn, attempt_id)
    signals = []
    latest_phase = phases[0] if phases else {}
    latest_summary = (latest_phase.get("summary") or "").lower()
    latest_status = latest_phase.get("status")
    if latest_status == "running" and "command" in latest_summary:
        signals.append("stale_command_snapshot")
    if commands:
        signals.append("durable_command_evidence")
    for phase in phases:
        phase_text = " ".join(
            [
                phase.get("status", ""),
                phase.get("summary", ""),
                phase.get("next_step", ""),
            ]
        ).lower()
        if "command exited" in phase_text:
            signals.append("command_exit_recorded")
            break
    status_text = (git_summary.get("git_status_short") or {}).get("stdout", "")
    diff_text = (git_summary.get("git_diff_stat") or {}).get("stdout", "")
    if status_text.strip():
        signals.append("dirty_worktree")
    if diff_text.strip():
        signals.append("diff_stat_available")
    # Logs are diagnostic evidence, not proof of completed work. In particular,
    # repeated error output must not authorize another model attempt. Durable
    # command records, checkpoints and worktree changes above establish progress.
    category = retry_policy.error_class(retry_policy.worker_error(logs))
    transport_failure = terminal_transport_failure(logs)
    status = "transport_interrupted" if transport_failure else "no_progress"
    if any(command["process_status"] in {"alive", "unknown"} for command in commands):
        status = "orphaned_command_running"
    elif signals:
        status = "recoverable_progress"
    return {
        "status": status,
        "task_id": task_id,
        "previous_attempt_id": attempt_id,
        "worktree_path": str(worktree_path or ""),
        "progress_signals": sorted(set(signals)),
        "latest_worker_phases": phases,
        "logs": logs,
        "git": git_summary,
        "commands": commands,
        "transport_failure": transport_failure,
        "rate_limit_failure": retry_policy.terminal_rate_limit(logs),
        "error_class": category,
    }


def inspect_attempt_recovery_with_metadata(
    conn,
    attempt_id,
    task_id,
    metadata,
    worktree_path,
):
    recovery = inspect_attempt_recovery(
        conn,
        attempt_id,
        task_id,
        metadata.get("dispatch", {}).get("worktree_path") or worktree_path,
    )
    if recovery.get("rate_limit_failure"):
        recorded = metadata.get("dispatch") or {}
        previous_fallback = (metadata.get("resume") or {}).get("model_fallback")
        fallback = recorded.get("fallback_model")
        if fallback and recorded.get("model") and fallback != recorded["model"] and not previous_fallback:
            recovery["model_fallback"] = {
                "worker_name": recorded.get("worker_name"),
                "model": fallback,
                "previous_model": recorded["model"],
                "reason_code": "rate_limited",
            }
            if recovery["status"] != "orphaned_command_running":
                recovery["status"] = "rate_limited"
        elif recovery["status"] != "orphaned_command_running":
            recovery["status"] = "unrecoverable"
        recovery["error_class"] = "rate_limited"
    return recovery


def inspection_due(state, now):
    return state.get("retryable", True) and (
        not state.get("next_retry_at") or retry_policy.timestamp(state["next_retry_at"]) <= retry_policy.timestamp(now)
    )
