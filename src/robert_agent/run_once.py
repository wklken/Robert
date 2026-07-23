#!/usr/bin/env python3
import argparse
from contextlib import closing
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from robert_agent import audit_result
from robert_agent import authorize
from robert_agent import dispatch
from robert_agent import discover
from robert_agent import render_prompt
from robert_agent import publish
from robert_agent import route
from robert_agent import route_config
from robert_agent import skills
from robert_agent import supervise
from robert_agent import validate_config
from robert_agent import workstream
from robert_agent import worktree
from robert_agent.common import emit
from robert_agent import project_memory, runtime_knowledge, storage, usage, wakeup, work_items


ROUTE_POLICIES = {
    policy["id"]: policy
    for policy in route_config.load_route_policies()
}


def _id(prefix):
    return f"{prefix}-{uuid4().hex[:12]}"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value):
    if isinstance(value, datetime):
        return value
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _insert_notification(conn, notification_type, status, run_now, metadata=None):
    metadata = metadata or {}
    conn.execute(
        """
        INSERT INTO notifications(
          notification_id, task_id, notification_type, channel, status, created_at, metadata_json
        )
        VALUES (?, ?, ?, 'local', ?, ?, ?)
        """,
        (
            _id("notification"),
            metadata.get("task_id"),
            notification_type,
            status,
            run_now,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        ),
    )


def _repo_id(full_name):
    return f"repo:{full_name}"


def _source_id(source_key):
    return f"source:{source_key}"


def _event_id(fingerprint):
    return f"event:{fingerprint}"


def _worker_result_script():
    return Path(__file__).resolve().parent / "worker_result.py"


def _worker_snapshot_script():
    return Path(__file__).resolve().parent / "worker_snapshot.py"


def _worker_heartbeat_script():
    return Path(__file__).resolve().parent / "worker_heartbeat.py"


def _agent_status_script():
    return Path(__file__).resolve().parent / "status.py"


def _json_object(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value):
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _tail_file(path, max_lines=80, max_bytes=32768):
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


def _run_inspection_command(command, cwd):
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=5,
        )
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


def _git_recovery_summary(worktree_path):
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
    status = _run_inspection_command(["git", "status", "--short"], worktree_path)
    diff_stat = _run_inspection_command(["git", "diff", "--stat"], worktree_path)
    branch = _run_inspection_command(["git", "branch", "--show-current"], worktree_path)
    return {
        "ok": status.get("ok", False),
        "worktree_path": str(worktree_path),
        "git_status_short": status,
        "git_diff_stat": diff_stat,
        "git_branch": branch,
    }


def _attempt_log_tails(conn, attempt_id):
    rows = conn.execute(
        """
        SELECT artifact_type, path
        FROM artifacts
        WHERE attempt_id = ?
          AND artifact_type IN ('worker_stdout', 'worker_stderr')
        ORDER BY created_at DESC, artifact_id DESC
        """,
        (attempt_id,),
    ).fetchall()
    tails = {}
    for artifact_type, path in rows:
        if artifact_type in tails:
            continue
        tails[artifact_type] = {
            "path": path,
            "tail": _tail_file(path),
        }
    return tails


def _latest_worker_phases(conn, attempt_id, limit=8):
    rows = conn.execute(
        """
        SELECT phase, status, summary, next_step, created_at
        FROM worker_phases
        WHERE attempt_id = ?
        ORDER BY created_at DESC, phase_id DESC
        LIMIT ?
        """,
        (attempt_id, limit),
    ).fetchall()
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


def _inspect_attempt_recovery(conn, attempt_id, task_id, worktree_path):
    phases = _latest_worker_phases(conn, attempt_id)
    logs = _attempt_log_tails(conn, attempt_id)
    git_summary = _git_recovery_summary(worktree_path)
    signals = []
    latest_phase = phases[0] if phases else {}
    latest_summary = (latest_phase.get("summary") or "").lower()
    latest_status = latest_phase.get("status")
    if latest_status == "running" and "command" in latest_summary:
        signals.append("active_command_snapshot")
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
    for artifact_type, payload in logs.items():
        tail = payload.get("tail", "")
        if not tail:
            continue
        if "command exited" in tail.lower():
            signals.append(f"{artifact_type}_command_exit")
        else:
            signals.append(f"{artifact_type}_tail_available")
    status = "no_progress"
    if latest_status == "running" and "command" in latest_summary:
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
    }


def _inspect_attempt_recovery_with_metadata(
    conn,
    attempt_id,
    task_id,
    metadata,
    worktree_path,
):
    recovery = _inspect_attempt_recovery(
        conn,
        attempt_id,
        task_id,
        metadata.get("dispatch", {}).get("worktree_path") or None,
    )
    if recovery["worktree_path"] == "":
        recovery = _inspect_attempt_recovery(conn, attempt_id, task_id, worktree_path)
    return recovery


def _safe_discovery_error(exc):
    if isinstance(exc, subprocess.CalledProcessError):
        cmd = exc.cmd if isinstance(exc.cmd, (list, tuple)) else [exc.cmd]
        command = [str(part) for part in cmd]
        return {
            "status": "failed_discovery",
            "safe_error": (
                f"GitHub discovery command failed with exit {exc.returncode}: "
                + " ".join(command)
            ),
            "returncode": exc.returncode,
            "command": command,
        }
    if isinstance(exc, json.JSONDecodeError):
        return {
            "status": "failed_discovery",
            "safe_error": "GitHub discovery returned invalid JSON",
            "message": exc.msg,
        }
    if isinstance(exc, (KeyError, TypeError, ValueError)):
        return {
            "status": "failed_discovery",
            "safe_error": f"GitHub discovery returned invalid event data: {exc}",
            "error_type": type(exc).__name__,
        }
    return {
        "status": "failed_discovery",
        "safe_error": "GitHub discovery failed",
    }


def _safe_dispatch_error(exc):
    return {
        "ok": False,
        "status": "failed_dispatch",
        "safe_error": f"worker dispatch failed: {exc}",
        "error_type": type(exc).__name__,
    }


def _safe_publish_error(exc):
    return {
        "ok": False,
        "status": "publish_failed",
        "safe_error": f"GitHub action publication failed: {exc}",
        "error_type": type(exc).__name__,
        "pending_count": 0,
        "published_count": 0,
        "deduplicated_count": 0,
        "skipped_count": 0,
        "failed_count": 1,
    }


def _safe_route_error(exc):
    if isinstance(exc, subprocess.CalledProcessError):
        cmd = exc.cmd if isinstance(exc.cmd, (list, tuple)) else [exc.cmd]
        command = [str(part) for part in cmd]
        return {
            "ok": False,
            "status": "failed_route",
            "safe_error": (
                f"worktree preparation command failed with exit {exc.returncode}: "
                + " ".join(command)
            ),
            "returncode": exc.returncode,
            "command": command,
        }
    return {
        "ok": False,
        "status": "failed_route",
        "safe_error": "worktree preparation failed",
        "error_type": type(exc).__name__,
    }


def _event_priority(event):
    event_at = event.get("event_at") or ""
    fingerprint = event.get("event_fingerprint") or ""
    if event.get("authorization_status") == "authorized_trigger":
        if event.get("source_type") == "pull_request" and event.get("has_open_dd_pr"):
            return (0, event_at, fingerprint)
        return (1, event_at, fingerprint)
    if _is_driving_context_event(event):
        return (2, event_at, fingerprint)
    return (3, event_at, fingerprint)


def _is_driving_context_event(event):
    return (
        event.get("authorization_status") in {"accepted_context", "accepted_review_participant"}
        and event.get("drives_execution")
    )


def _insert_run_step(conn, run_id, key, status="pending"):
    conn.execute(
        """
        INSERT INTO run_steps(step_id, run_id, step_key, status)
        VALUES (?, ?, ?, ?)
        """,
        (_id("step"), run_id, key, status),
    )


def _mark_run_step(conn, run_id, key, status, run_now=None, output=None, error=None):
    run_now = run_now or _now()
    output_json = json.dumps(output, ensure_ascii=False, sort_keys=True) if output is not None else None
    error_json = json.dumps(error, ensure_ascii=False, sort_keys=True) if error is not None else None
    finished_at = run_now if status in {"succeeded", "failed", "skipped"} else None
    conn.execute(
        """
        UPDATE run_steps
        SET status = ?,
            started_at = COALESCE(started_at, ?),
            finished_at = ?,
            output_json = COALESCE(?, output_json),
            error_json = COALESCE(?, error_json)
        WHERE run_id = ?
          AND step_key = ?
        """,
        (status, run_now, finished_at, output_json, error_json, run_id, key),
    )


def _insert_repo_run_step(conn, run_id, repo_id, key, status="pending"):
    conn.execute(
        """
        INSERT OR IGNORE INTO run_repo_steps(step_id, run_id, repo_id, step_key, status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (_id("repo-step"), run_id, repo_id, key, status),
    )


def _mark_repo_run_step(conn, run_id, repo_id, key, status, run_now=None, output=None, error=None):
    run_now = run_now or _now()
    finished_at = run_now if status in {"succeeded", "failed", "skipped"} else None
    output_json = json.dumps(output, ensure_ascii=False, sort_keys=True) if output is not None else None
    error_json = json.dumps(error, ensure_ascii=False, sort_keys=True) if error is not None else None
    conn.execute(
        """
        UPDATE run_repo_steps
        SET status = ?,
            started_at = COALESCE(started_at, ?),
            finished_at = COALESCE(?, finished_at),
            output_json = COALESCE(?, output_json),
            error_json = COALESCE(?, error_json)
        WHERE run_id = ?
          AND repo_id = ?
          AND step_key = ?
        """,
        (status, run_now, finished_at, output_json, error_json, run_id, repo_id, key),
    )


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


def _with_python_bin(repo, config_result):
    return {**repo, "python_bin": config_result["python_bin"]}


def _init_repo_steps(conn, run_id, repo_id):
    for step in REPO_STEP_ORDER:
        _insert_repo_run_step(conn, run_id, repo_id, step)


def _upsert_repo(conn, repo):
    repo_id = _repo_id(repo["full_name"])
    conn.execute(
        """
        INSERT INTO repos(
          repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root, enabled
        )
        VALUES (?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(repo_id) DO UPDATE SET
          full_name = excluded.full_name,
          github_account = excluded.github_account,
          default_base_branch = excluded.default_base_branch,
          repo_root = excluded.repo_root,
          worktree_root = excluded.worktree_root,
          enabled = excluded.enabled
        """,
        (
            repo_id,
            repo["full_name"],
            repo["github_account"],
            repo["default_base_branch"],
            repo["repo_root"],
            repo["worktree_root"],
        ),
    )
    return repo_id


def _acquire_agent_lease(conn, repo_id, run_id, run_now, ttl_minutes):
    resource_key = repo_id
    now_dt = _parse_time(run_now)
    expires_at = (now_dt + timedelta(minutes=ttl_minutes)).isoformat()
    conn.execute(
        """
        UPDATE leases
        SET status = 'expired'
        WHERE resource_type = 'agent_run'
          AND resource_key = ?
          AND status = 'active'
          AND expires_at <= ?
        """,
        (resource_key, run_now),
    )
    active = conn.execute(
        """
        SELECT lease_id, owner_id, expires_at
        FROM leases
        WHERE resource_type = 'agent_run'
          AND resource_key = ?
          AND status = 'active'
        """,
        (resource_key,),
    ).fetchone()
    if active:
        return {
            "ok": False,
            "status": "skipped_active_lease",
            "lease_id": active[0],
            "owner_id": active[1],
            "expires_at": active[2],
        }

    lease_id = _id("lease")
    conn.execute(
        """
        INSERT INTO leases(
          lease_id, resource_type, resource_key, owner_id, status,
          acquired_at, expires_at, heartbeat_at
        )
        VALUES (?, 'agent_run', ?, ?, 'active', ?, ?, ?)
        """,
        (lease_id, resource_key, run_id, run_now, expires_at, run_now),
    )
    return {"ok": True, "status": "acquired", "lease_id": lease_id}


def _release_agent_lease(conn, lease_id, run_now):
    if not lease_id:
        return
    conn.execute(
        """
        UPDATE leases
        SET status = 'released', released_at = ?, heartbeat_at = ?
        WHERE lease_id = ?
        """,
        (run_now, run_now, lease_id),
    )


def _supervise_running_attempts(conn, repo_id, config_result, run_now, data_dir=None, db_path=None, repo=None):
    rows = conn.execute(
        """
        SELECT
          a.attempt_id,
          a.task_id,
          t.workstream_id,
          a.heartbeat_at,
          a.started_at,
          a.status,
          a.metadata_json
        FROM attempts a
        JOIN tasks t ON t.task_id = a.task_id
        JOIN workstreams w ON w.workstream_id = t.workstream_id
        WHERE w.repo_id = ?
          AND a.status IN ('running', 'stale')
          AND t.lifecycle IN ('queued', 'running')
        """,
        (repo_id,),
    ).fetchall()
    supervised = 0
    now_dt = _parse_time(run_now)
    startup_grace_seconds = config_result.get("worker_startup_grace_seconds", 300)
    for (
        attempt_id,
        task_id,
        workstream_id,
        heartbeat_at,
        started_at,
        attempt_status,
        metadata_json,
    ) in rows:
        if not heartbeat_at or not started_at:
            continue
        metadata = _json_object(metadata_json)
        status = supervise.classify_attempt(
            heartbeat_at=heartbeat_at,
            started_at=started_at,
            now=now_dt,
            stale_after_minutes=config_result.get("stale_after_minutes", 20),
            hard_timeout_minutes=config_result.get("hard_timeout_minutes", 90),
        )
        process_status = {"status": "not_checked"}
        if (
            attempt_status in {"running", "stale"}
            and status["status"] in {"running", "stale", "failed_timeout"}
        ):
            process_status = _dispatch_process_status(metadata)
        if process_status["status"] == "not_found":
            worktree_row = conn.execute(
                "SELECT worktree_path FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            recovery = _inspect_attempt_recovery_with_metadata(
                conn,
                attempt_id,
                task_id,
                metadata,
                worktree_row[0] if worktree_row else None,
            )
            if (
                recovery["status"] == "orphaned_command_running"
                and status["status"] != "failed_timeout"
            ):
                supervised += 1
                metadata["supervise"] = {
                    **process_status,
                    "ok": True,
                    "status": "orphaned_command_running",
                    "recovery": recovery,
                }
                conn.execute(
                    """
                    UPDATE attempts
                    SET metadata_json = ?
                    WHERE attempt_id = ?
                    """,
                    (json.dumps(metadata, ensure_ascii=False, sort_keys=True), attempt_id),
                )
                continue
            if (
                recovery["status"] == "recoverable_progress"
                and data_dir is not None
                and db_path is not None
                and repo is not None
            ):
                supervised += 1
                status = {
                    **process_status,
                    "ok": False,
                    "status": "needs_resume",
                    "original_status": (
                        "failed_timeout"
                        if status["status"] == "failed_timeout"
                        else "failed_worker_process_exited"
                    ),
                    "terminate": False,
                    "recovery": recovery,
                }
                metadata["supervise"] = status
                resume = _create_resume_attempt_and_prompt(
                    conn,
                    Path(data_dir),
                    Path(db_path),
                    repo,
                    repo_id,
                    task_id,
                    workstream_id,
                    attempt_id,
                    run_now,
                    recovery,
                )
                if resume:
                    metadata["resume"] = resume
                    conn.execute(
                        """
                        UPDATE attempts
                        SET status = 'failed', finished_at = ?, failure_json = ?, metadata_json = ?
                        WHERE attempt_id = ?
                        """,
                        (
                            run_now,
                            json.dumps(status, sort_keys=True),
                            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                            attempt_id,
                        ),
                    )
                    _insert_notification(
                        conn,
                        "worker_resume_prepared",
                        "recorded",
                        run_now,
                        {
                            "task_id": task_id,
                            "attempt_id": attempt_id,
                            "resume_attempt_id": resume["attempt_id"],
                            "workstream_id": workstream_id,
                            "recovery_artifact_path": resume["recovery_artifact_path"],
                        },
                    )
                    continue
            if status["status"] == "failed_timeout":
                status = {**status, "process": process_status, "recovery": recovery}
            else:
                status = {
                    **process_status,
                    "ok": False,
                    "status": "failed_worker_process_exited",
                    "terminate": False,
                }
        runtime_seconds = (now_dt - _parse_time(started_at)).total_seconds()
        has_worker_phase = conn.execute(
            "SELECT 1 FROM worker_phases WHERE attempt_id = ? LIMIT 1",
            (attempt_id,),
        ).fetchone()
        if (
            status["status"] in {"running", "stale"}
            and runtime_seconds >= startup_grace_seconds
            and not has_worker_phase
        ):
            status = {
                "ok": False,
                "status": "failed_worker_startup",
                "terminate": True,
                "startup_grace_seconds": startup_grace_seconds,
                "runtime_seconds": runtime_seconds,
            }
        metadata["supervise"] = status
        if status["status"] == "running":
            if attempt_status == "stale":
                supervised += 1
                conn.execute(
                    """
                    UPDATE attempts
                    SET status = 'running', metadata_json = ?
                    WHERE attempt_id = ?
                    """,
                    (json.dumps(metadata, ensure_ascii=False, sort_keys=True), attempt_id),
                )
            continue
        supervised += 1
        if status["status"] == "stale":
            conn.execute(
                """
                UPDATE attempts
                SET status = 'stale', metadata_json = ?
                WHERE attempt_id = ?
                """,
                (json.dumps(metadata, ensure_ascii=False, sort_keys=True), attempt_id),
            )
            if attempt_status != "stale":
                _insert_notification(
                    conn,
                    "worker_stale",
                    "recorded",
                    run_now,
                    {"task_id": task_id, "attempt_id": attempt_id, "workstream_id": workstream_id},
                )
            continue
        metadata["termination"] = _terminate_attempt_process(metadata)
        conn.execute(
            """
            UPDATE attempts
            SET status = 'failed', finished_at = ?, failure_json = ?, metadata_json = ?
            WHERE attempt_id = ?
            """,
            (
                run_now,
                json.dumps(status, sort_keys=True),
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                attempt_id,
            ),
        )
        _finalize_failed_task(conn, task_id, workstream_id, run_now, status)
        _insert_notification(
            conn,
            _supervision_failure_notification_type(status["status"]),
            "recorded",
            run_now,
            {"task_id": task_id, "attempt_id": attempt_id, "workstream_id": workstream_id},
        )
    return supervised


def _recover_failed_timeout_attempts(
    conn,
    repo_id,
    run_now,
    data_dir=None,
    db_path=None,
    repo=None,
):
    if data_dir is None or db_path is None or repo is None:
        return 0
    rows = conn.execute(
        """
        SELECT
          a.attempt_id,
          a.task_id,
          t.workstream_id,
          a.worktree_path,
          a.failure_json,
          a.metadata_json
        FROM attempts a
        JOIN tasks t ON t.task_id = a.task_id
        JOIN workstreams w ON w.workstream_id = t.workstream_id
        WHERE w.repo_id = ?
          AND a.status = 'failed'
          AND t.lifecycle = 'failed'
          AND w.lifecycle = 'failed'
          AND (a.finished_at IS NULL OR a.finished_at < ?)
          AND a.attempt_no = (
            SELECT MAX(a2.attempt_no)
            FROM attempts a2
            WHERE a2.task_id = a.task_id
          )
        """,
        (repo_id, run_now),
    ).fetchall()
    recovered = 0
    for (
        attempt_id,
        task_id,
        workstream_id,
        worktree_path,
        failure_json,
        metadata_json,
    ) in rows:
        failure = _json_object(failure_json)
        if failure.get("status") != "failed_timeout":
            continue
        metadata = _json_object(metadata_json)
        process_status = _dispatch_process_status(metadata)
        if process_status["status"] != "not_found":
            continue
        recovery = _inspect_attempt_recovery_with_metadata(
            conn,
            attempt_id,
            task_id,
            metadata,
            worktree_path,
        )
        if recovery["status"] != "recoverable_progress":
            continue
        status = {
            **process_status,
            "ok": False,
            "status": "needs_resume",
            "original_status": "failed_timeout",
            "terminate": False,
            "recovery": recovery,
        }
        metadata["supervise"] = status
        resume = _create_resume_attempt_and_prompt(
            conn,
            Path(data_dir),
            Path(db_path),
            repo,
            repo_id,
            task_id,
            workstream_id,
            attempt_id,
            run_now,
            recovery,
        )
        if not resume:
            continue
        recovered += 1
        metadata["resume"] = resume
        conn.execute(
            """
            UPDATE attempts
            SET finished_at = ?, failure_json = ?, metadata_json = ?
            WHERE attempt_id = ?
            """,
            (
                run_now,
                json.dumps(status, sort_keys=True),
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                attempt_id,
            ),
        )
        _insert_notification(
            conn,
            "worker_resume_prepared",
            "recorded",
            run_now,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "resume_attempt_id": resume["attempt_id"],
                "workstream_id": workstream_id,
                "recovery_artifact_path": resume["recovery_artifact_path"],
            },
        )
    return recovered


def _dispatch_process_status(metadata):
    dispatch_metadata = (metadata or {}).get("dispatch") or {}
    pid = dispatch_metadata.get("pid")
    if pid is None:
        return {"status": "unknown", "reason": "missing_pid"}
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {"status": "unknown", "reason": "invalid_pid", "pid": pid}
    if pid <= 0:
        return {"status": "unknown", "reason": "invalid_pid", "pid": pid}
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {"status": "not_found", "pid": pid}
    except PermissionError:
        return {"status": "alive", "pid": pid, "visibility": "permission_denied"}

    process_state = _process_state(pid)
    if process_state in {"Z", "X", "x"}:
        return {
            "status": "not_found",
            "pid": pid,
            "reason": "zombie",
            "process_state": process_state,
        }
    return {"status": "alive", "pid": pid}


def _supervision_failure_notification_type(status):
    if status == "failed_worker_startup":
        return "worker_startup_failed"
    if status == "failed_worker_process_exited":
        return "worker_process_exited"
    return "worker_timeout"


def _terminate_attempt_process(metadata):
    dispatch_metadata = (metadata or {}).get("dispatch") or {}
    pid = dispatch_metadata.get("pid")
    if pid is None:
        return {"status": "not_signalled", "reason": "missing_pid"}
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {"status": "not_signalled", "reason": "invalid_pid", "pid": pid}
    if pid <= 0:
        return {"status": "not_signalled", "reason": "invalid_pid", "pid": pid}
    process_state = _process_state(pid)
    if process_state in {"Z", "X", "x"}:
        return {
            "status": "not_found",
            "reason": "zombie",
            "pid": pid,
            "process_state": process_state,
            "signal": "SIGTERM",
        }
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"status": "not_found", "pid": pid, "signal": "SIGTERM"}
    except PermissionError:
        return {"status": "permission_denied", "pid": pid, "signal": "SIGTERM"}
    return {"status": "signalled", "pid": pid, "signal": "SIGTERM"}


def _proc_process_state(pid):
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return None
    try:
        remainder = stat_text.rsplit(")", 1)[1].strip()
    except IndexError:
        return None
    fields = remainder.split()
    return fields[0] if fields else None


def _ps_process_state(pid):
    try:
        completed = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    stat = (completed.stdout or "").strip().split()
    return stat[0][0] if stat and stat[0] else None


def _process_state(pid):
    return _proc_process_state(pid) or _ps_process_state(pid)


def _load_active_workstreams(conn, repo_id):
    return {
        row[0]
        for row in conn.execute(
            """
            SELECT workstream_id
            FROM workstreams
            WHERE repo_id = ?
              AND lifecycle IN ('active', 'waiting_for_user')
              AND active_task_id IS NOT NULL
            """,
            (repo_id,),
        )
    }


def _load_known_workstreams(conn, repo_id):
    return {
        row[0]
        for row in conn.execute(
            """
            SELECT workstream_id
            FROM workstreams
            WHERE repo_id = ?
              AND lifecycle != 'canceled'
            """,
            (repo_id,),
        )
    }


def _load_review_participants(conn, repo_id):
    participants = {}
    rows = conn.execute(
        """
        SELECT workstream_id, metadata_json
        FROM workstreams
        WHERE repo_id = ?
          AND lifecycle != 'canceled'
        """,
        (repo_id,),
    ).fetchall()
    for workstream_id, metadata_json in rows:
        metadata = _json_object(metadata_json)
        review_participants = metadata.get("review_participants")
        if not isinstance(review_participants, list):
            continue
        scoped = [login for login in review_participants if isinstance(login, str) and login]
        if scoped:
            participants[workstream_id] = scoped
    return participants


def _attach_review_participants(events, review_participants_by_workstream):
    enriched = []
    for event in events:
        participants = review_participants_by_workstream.get(event.get("workstream_id"))
        if participants:
            enriched.append({**event, "review_participants": participants})
        else:
            enriched.append(event)
    return enriched


def _record_review_participant(conn, workstream_id, event, route_result, run_now):
    if route_result.get("route_id") != "review-pr":
        return
    pr_author = event.get("pr_author_login")
    if not pr_author:
        return
    row = conn.execute(
        "SELECT metadata_json FROM workstreams WHERE workstream_id = ?",
        (workstream_id,),
    ).fetchone()
    metadata = _json_object(row[0] if row else None)
    participants = metadata.get("review_participants")
    if not isinstance(participants, list):
        participants = []
    if pr_author not in participants:
        participants.append(pr_author)
    metadata["review_participants"] = participants
    metadata["review_authorized_by"] = event.get("requester_login") or event.get("actor_login") or ""
    metadata["review_authorized_event"] = event.get("event_fingerprint") or ""
    metadata["review_authorized_at"] = event.get("event_at") or run_now
    conn.execute(
        """
        UPDATE workstreams
        SET metadata_json = ?,
            updated_at = ?
        WHERE workstream_id = ?
        """,
        (
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            run_now,
            workstream_id,
        ),
    )


def _remote_source_state(repo_full_name, source_type, number, runner):
    issue_payload = discover._try_run_json(
        ["gh", "api", f"repos/{repo_full_name}/issues/{number}"],
        runner=runner,
    )
    if not isinstance(issue_payload, dict):
        return None
    remote = {
        "state": issue_payload.get("state") or "open",
        "state_reason": issue_payload.get("state_reason"),
        "title": issue_payload.get("title") or "",
        "html_url": issue_payload.get("html_url"),
        "updated_at": issue_payload.get("updated_at"),
        "closed_at": issue_payload.get("closed_at"),
        "merged": False,
        "merged_at": None,
    }
    if source_type != "pull_request":
        return remote
    pr_payload = discover._try_run_json(
        ["gh", "api", f"repos/{repo_full_name}/pulls/{number}"],
        runner=runner,
    )
    if isinstance(pr_payload, dict):
        remote["html_url"] = pr_payload.get("html_url") or remote["html_url"]
        remote["merged"] = bool(pr_payload.get("merged") or pr_payload.get("merged_at"))
        remote["merged_at"] = pr_payload.get("merged_at")
    return remote


def _remote_terminal_state(source_type, remote_state):
    if (remote_state.get("state") or "").lower() != "closed":
        return None
    if source_type == "pull_request":
        if remote_state.get("merged"):
            return "completed", "remote_pr_merged"
        return "canceled", "remote_pr_closed"
    if remote_state.get("state_reason") == "completed":
        return "completed", "remote_source_closed"
    return "canceled", "remote_source_closed"


def _record_remote_terminal_state(
    conn,
    repo_id,
    workstream_id,
    source_id,
    source_key,
    source_type,
    number,
    task_id,
    remote_state,
    workstream_lifecycle,
    audit_type,
    run_now,
):
    source_metadata_row = conn.execute(
        "SELECT metadata_json FROM github_sources WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    source_metadata = _json_object(source_metadata_row[0] if source_metadata_row else None)
    source_metadata["remote_state"] = remote_state
    conn.execute(
        """
        UPDATE github_sources
        SET state = ?,
            html_url = COALESCE(?, html_url),
            title = COALESCE(NULLIF(?, ''), title),
            source_updated_at = COALESCE(?, source_updated_at),
            metadata_json = ?
        WHERE source_id = ?
        """,
        (
            remote_state.get("state") or "closed",
            remote_state.get("html_url"),
            remote_state.get("title") or "",
            remote_state.get("updated_at"),
            json.dumps(source_metadata, ensure_ascii=False, sort_keys=True),
            source_id,
        ),
    )
    remote_payload = {
        "status": audit_type,
        "source_key": source_key,
        "source_type": source_type,
        "number": number,
        "remote_state": remote_state,
    }
    conn.execute(
        """
        UPDATE tasks
        SET lifecycle = 'canceled',
            updated_at = ?,
            metadata_json = ?
        WHERE task_id = ?
          AND lifecycle IN ('detected', 'authorized', 'classified', 'queued', 'running', 'waiting_for_user')
        """,
        (
            run_now,
            json.dumps({"remote_reconciliation": remote_payload}, ensure_ascii=False, sort_keys=True),
            task_id,
        ),
    )
    attempt_metadata_json = json.dumps(
        {"remote_reconciliation": remote_payload},
        ensure_ascii=False,
        sort_keys=True,
    )
    conn.execute(
        """
        UPDATE attempts
        SET status = 'canceled',
            finished_at = ?,
            failure_json = ?,
            metadata_json = ?
        WHERE task_id = ?
          AND status IN ('prepared', 'running', 'stale')
        """,
        (
            run_now,
            json.dumps(remote_payload, ensure_ascii=False, sort_keys=True),
            attempt_metadata_json,
            task_id,
        ),
    )
    workstream_metadata_row = conn.execute(
        "SELECT metadata_json FROM workstreams WHERE workstream_id = ?",
        (workstream_id,),
    ).fetchone()
    workstream_metadata = _json_object(workstream_metadata_row[0] if workstream_metadata_row else None)
    workstream_metadata["remote_reconciliation"] = remote_payload
    conn.execute(
        """
        UPDATE workstreams
        SET lifecycle = ?,
            active_task_id = NULL,
            updated_at = ?,
            metadata_json = ?
        WHERE workstream_id = ?
        """,
        (
            workstream_lifecycle,
            run_now,
            json.dumps(workstream_metadata, ensure_ascii=False, sort_keys=True),
            workstream_id,
        ),
    )
    conn.execute(
        """
        INSERT INTO audit_events(
          audit_id, repo_id, workstream_id, task_id, event_type, created_at, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _id("audit"),
            repo_id,
            workstream_id,
            task_id,
            audit_type,
            run_now,
            json.dumps(remote_payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    item = work_items.resolve_work_item_for_workstream(conn, workstream_id)
    if item and source_type == "pull_request":
        if audit_type == "remote_pr_merged":
            work_items.record_system_event(
                conn,
                item["work_item_id"],
                event_type="pr_merged",
                idempotency_key=f"pr-merged:{source_key}:{remote_state.get('merged_at') or run_now}",
                metadata={"source_key": source_key, "number": number},
                now=run_now,
            )
            conn.execute(
                """
                UPDATE work_items
                SET completed_at = ?, updated_at = ?, version = version + 1
                WHERE work_item_id = ? AND completed_at IS NULL
                """,
                (remote_state.get("merged_at") or run_now, run_now, item["work_item_id"]),
            )
            conn.execute(
                """
                UPDATE workstreams
                SET lifecycle = 'completed', active_task_id = NULL, updated_at = ?
                WHERE workstream_id = (SELECT workstream_id FROM work_items WHERE work_item_id = ?)
                """,
                (run_now, item["work_item_id"]),
            )
        elif audit_type == "remote_pr_closed":
            work_items.record_system_event(
                conn,
                item["work_item_id"],
                event_type="unmerged_pr_closed",
                idempotency_key=f"pr-closed-unmerged:{source_key}:{remote_state.get('closed_at') or run_now}",
                body="Pull request closed without merge.",
                metadata={"source_key": source_key, "number": number},
                now=run_now,
            )


def _current_state_terminal_reason(event):
    state = (event.get("state") or "").lower()
    merged = bool(event.get("merged") or event.get("merged_at"))
    if state != "closed" and not merged:
        return None
    remote_state = {
        "state": "closed" if merged else state,
        "state_reason": event.get("state_reason"),
        "title": event.get("title") or "",
        "html_url": event.get("url"),
        "updated_at": event.get("source_updated_at") or event.get("event_at"),
        "closed_at": event.get("closed_at"),
        "merged": merged,
        "merged_at": event.get("merged_at"),
    }
    terminal = _remote_terminal_state(event.get("source_type"), remote_state)
    if not terminal:
        return None
    _workstream_lifecycle, terminal_reason = terminal
    return terminal_reason, remote_state


def _source_has_active_unpublished_result(conn, source_id):
    row = conn.execute(
        """
        SELECT 1
        FROM workstreams w
        LEFT JOIN workstream_sources ws ON ws.workstream_id = w.workstream_id
        JOIN tasks t ON t.workstream_id = w.workstream_id
        JOIN worker_results wr ON wr.task_id = t.task_id
        LEFT JOIN github_actions ga ON ga.result_id = wr.result_id
        WHERE (w.primary_source_id = ? OR ws.source_id = ?)
          AND (
            t.lifecycle IN ('queued', 'running', 'waiting_for_user')
            OR json_extract(wr.metadata_json, '$.audit.status') IS NULL
            OR (
              ga.action_id IS NOT NULL
              AND ga.audit_status = 'accepted'
              AND ga.publish_status != 'published'
            )
          )
        LIMIT 1
        """,
        (source_id, source_id),
    ).fetchone()
    return bool(row)


def _record_current_state_closed_source(
    conn,
    repo_id,
    source_id,
    event_id,
    event,
    terminal_reason,
    remote_state,
    run_now,
):
    source_metadata_row = conn.execute(
        "SELECT metadata_json FROM github_sources WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    source_metadata = _json_object(source_metadata_row[0] if source_metadata_row else None)
    payload = {
        "status": "current_state_closed",
        "source_key": event["source_key"],
        "source_type": event["source_type"],
        "number": event["number"],
        "terminal_reason": terminal_reason,
        "skip_reason": "source_already_closed_before_dispatch",
        "remote_state": remote_state,
    }
    source_metadata["current_state_reconciliation"] = payload
    conn.execute(
        """
        UPDATE github_sources
        SET state = 'closed',
            html_url = COALESCE(?, html_url),
            title = COALESCE(NULLIF(?, ''), title),
            source_updated_at = COALESCE(?, source_updated_at),
            metadata_json = ?
        WHERE source_id = ?
        """,
        (
            remote_state.get("html_url"),
            remote_state.get("title") or "",
            remote_state.get("updated_at"),
            json.dumps(source_metadata, ensure_ascii=False, sort_keys=True),
            source_id,
        ),
    )
    conn.execute(
        """
        UPDATE github_events
        SET authorization_status = 'ignored_current_state_closed'
        WHERE event_id = ?
        """,
        (event_id,),
    )
    conn.execute(
        """
        INSERT INTO audit_events(
          audit_id, repo_id, workstream_id, task_id, event_type, created_at, payload_json
        )
        VALUES (?, ?, NULL, NULL, 'current_state_closed', ?, ?)
        """,
        (
            _id("audit"),
            repo_id,
            run_now,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    return payload


def _record_current_state_closed_source_if_needed(conn, repo_id, source_id, event_id, event, run_now):
    terminal = _current_state_terminal_reason(event)
    if not terminal:
        return None
    if _source_has_active_unpublished_result(conn, source_id):
        return None
    terminal_reason, remote_state = terminal
    return _record_current_state_closed_source(
        conn,
        repo_id,
        source_id,
        event_id,
        event,
        terminal_reason,
        remote_state,
        run_now,
    )


def _reconcile_remote_source_states(conn, repo_id, repo, runner, run_now):
    rows = conn.execute(
        """
        SELECT
          w.workstream_id,
          w.active_task_id,
          gs.source_id,
          gs.source_key,
          gs.source_type,
          gs.number
        FROM workstreams w
        JOIN github_sources gs ON gs.source_id = w.primary_source_id
        WHERE w.repo_id = ?
          AND (
            (
              w.active_task_id IS NOT NULL
              AND w.lifecycle IN ('active', 'waiting_for_user')
            )
            OR (
              gs.source_type = 'pull_request'
              AND gs.state = 'open'
            )
          )
        ORDER BY w.updated_at, w.workstream_id
        """,
        (repo_id,),
    ).fetchall()
    reconciled = 0
    for workstream_id, task_id, source_id, source_key, source_type, number in rows:
        task_id = task_id or _latest_task_id(conn, workstream_id)
        remote_state = _remote_source_state(repo["full_name"], source_type, number, runner)
        if not remote_state:
            continue
        terminal = _remote_terminal_state(source_type, remote_state)
        if not terminal:
            continue
        workstream_lifecycle, audit_type = terminal
        _record_remote_terminal_state(
            conn,
            repo_id,
            workstream_id,
            source_id,
            source_key,
            source_type,
            number,
            task_id,
            remote_state,
            workstream_lifecycle,
            audit_type,
            run_now,
        )
        reconciled += 1
    return reconciled


def _latest_task_id(conn, workstream_id):
    row = conn.execute(
        """
        SELECT task_id
        FROM tasks
        WHERE workstream_id = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (workstream_id,),
    ).fetchone()
    return row[0] if row else None


def _running_attempt_count(conn, repo_id):
    return conn.execute(
        """
        SELECT COUNT(*)
        FROM attempts a
        JOIN tasks t ON t.task_id = a.task_id
        JOIN workstreams w ON w.workstream_id = t.workstream_id
        WHERE w.repo_id = ?
          AND a.status = 'running'
          AND t.lifecycle IN ('queued', 'running')
        """,
        (repo_id,),
    ).fetchone()[0]


def _running_attempt_count_for_repos(conn, repo_ids):
    repo_ids = [repo_id for repo_id in repo_ids if repo_id]
    if not repo_ids:
        return 0
    placeholders = ",".join("?" for _repo_id in repo_ids)
    return conn.execute(
        f"""
        SELECT COUNT(*)
        FROM attempts a
        JOIN tasks t ON t.task_id = a.task_id
        JOIN workstreams w ON w.workstream_id = t.workstream_id
        WHERE w.repo_id IN ({placeholders})
          AND a.status = 'running'
          AND t.lifecycle IN ('queued', 'running')
        """,
        repo_ids,
    ).fetchone()[0]


def _prepared_attempt_dispatch_priority(conn, attempt_id):
    row = conn.execute(
        """
        SELECT t.route_id, t.workstream_id
        FROM attempts a
        JOIN tasks t ON t.task_id = a.task_id
        WHERE a.attempt_id = ?
        LIMIT 1
        """,
        (attempt_id,),
    ).fetchone()
    if not row:
        return 2
    route_id, workstream_id = row
    if route_id == "update-existing-pr":
        return 0
    if isinstance(workstream_id, str) and "!" in workstream_id:
        return 1
    return 2


def _active_task_id(conn, workstream_id):
    row = conn.execute(
        "SELECT active_task_id FROM workstreams WHERE workstream_id = ?",
        (workstream_id,),
    ).fetchone()
    if row and row[0]:
        return row[0]
    row = conn.execute(
        """
        SELECT task_id
        FROM tasks
        WHERE workstream_id = ?
          AND lifecycle IN ('detected', 'authorized', 'classified', 'queued', 'running')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (workstream_id,),
    ).fetchone()
    return row[0] if row else None


def _task_has_event(conn, task_id, event_fingerprint):
    row = conn.execute(
        """
        SELECT 1
        FROM task_events te
        JOIN github_events ge ON ge.event_id = te.event_id
        WHERE te.task_id = ?
          AND ge.event_fingerprint = ?
        LIMIT 1
        """,
        (task_id, event_fingerprint),
    ).fetchone()
    return bool(row)


def _event_has_terminal_trigger(conn, event_fingerprint):
    row = conn.execute(
        """
        SELECT 1
        FROM task_events te
        JOIN github_events ge ON ge.event_id = te.event_id
        JOIN tasks t ON t.task_id = te.task_id
        WHERE ge.event_fingerprint = ?
          AND te.relationship IN ('trigger', 'consumed')
          AND t.lifecycle IN ('completed', 'failed', 'canceled', 'ignored')
        LIMIT 1
        """,
        (event_fingerprint,),
    ).fetchone()
    return bool(row)


def _active_task_state(conn, workstream_id):
    row = conn.execute(
        """
        SELECT t.task_id, t.lifecycle, t.route_id, t.expected_output,
               a.attempt_id, a.status
        FROM tasks t
        LEFT JOIN attempts a ON a.task_id = t.task_id
        WHERE t.workstream_id = ?
          AND t.lifecycle IN ('detected', 'authorized', 'classified', 'queued', 'running')
        ORDER BY t.created_at DESC, a.attempt_no DESC
        LIMIT 1
        """,
        (workstream_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "task_id": row[0],
        "task_lifecycle": row[1],
        "route_id": row[2],
        "expected_output": row[3],
        "attempt_id": row[4],
        "attempt_status": row[5],
    }


def _waiting_task_state(conn, workstream_id):
    row = conn.execute(
        """
        SELECT t.task_id
        FROM workstreams w
        JOIN tasks t ON t.task_id = w.active_task_id
        WHERE w.workstream_id = ?
          AND w.lifecycle = 'waiting_for_user'
          AND t.lifecycle = 'waiting_for_user'
        LIMIT 1
        """,
        (workstream_id,),
    ).fetchone()
    if not row:
        return None
    return {"task_id": row[0]}


def _is_trusted_actor(event, repo):
    return event.get("actor_login") in set(repo.get("trusted_actors") or [])


def _is_dd_pr_followup_event(event):
    metadata = event.get("metadata") or {}
    return (
        event.get("source_type") == "pull_request"
        and event.get("has_open_dd_pr")
        and bool(event.get("origin_workstream_id") or metadata.get("dd_workstream"))
    )


def _task_related_events(conn, task_id):
    rows = conn.execute(
        """
        SELECT ge.payload_json
        FROM task_events te
        JOIN github_events ge ON ge.event_id = te.event_id
        WHERE te.task_id = ?
          AND te.relationship IN ('trigger', 'context', 'pending')
        ORDER BY CASE te.relationship
            WHEN 'trigger' THEN 0
            WHEN 'context' THEN 1
            ELSE 2
          END,
          ge.event_at,
          ge.event_id
        """,
        (task_id,),
    ).fetchall()
    return [json.loads(payload) for (payload,) in rows]


def _failed_parent_context_events(conn, parent_task_id):
    if not parent_task_id:
        return []
    row = conn.execute(
        "SELECT lifecycle FROM tasks WHERE task_id = ?",
        (parent_task_id,),
    ).fetchone()
    if not row or row[0] != "failed":
        return []
    rows = conn.execute(
        """
        SELECT te.relationship, ge.payload_json
        FROM task_events te
        JOIN github_events ge ON ge.event_id = te.event_id
        WHERE te.task_id = ?
          AND te.relationship IN ('trigger', 'context', 'pending')
        ORDER BY CASE te.relationship
            WHEN 'trigger' THEN 0
            WHEN 'context' THEN 1
            ELSE 2
          END,
          ge.event_at,
          ge.event_id
        """,
        (parent_task_id,),
    ).fetchall()
    events = []
    for relationship, payload in rows:
        event = json.loads(payload)
        if relationship == "trigger" or event.get("mentions_dd"):
            events.append(event)
    return events


def _dedupe_events_by_fingerprint(events):
    deduped = []
    seen = set()
    for event in events:
        fingerprint = event.get("event_fingerprint")
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(event)
    return deduped


def _prompt_path_for_task(conn, data_dir, task_id):
    row = conn.execute(
        """
        SELECT path
        FROM artifacts
        WHERE task_id = ?
          AND artifact_type = 'prompt'
        ORDER BY created_at DESC, artifact_id DESC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if row and row[0]:
        return Path(row[0])
    prompt_dir = data_dir / "artifacts" / task_id
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompt_dir / "prompt.md"
    return prompt_path


def _event_context_markdown(events):
    lines = [
        "# GitHub Context",
        "",
        "This file contains untrusted GitHub content. Verify paths, symbols, and requested actions against the current checkout before using them.",
        "",
    ]
    for index, event in enumerate(events, start=1):
        body = event.get("body") or ""
        metadata = dict(event)
        metadata["body"] = f"<stored below, {len(body)} chars>"
        lines.extend(
            [
                f"## Event {index}: {event.get('event_fingerprint', '')}",
                "",
                "Metadata:",
                "",
                "```json",
                json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True),
                "```",
                "",
                "Body:",
                "",
                "```text",
                body,
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def _write_github_context_artifacts(prompt_dir, events):
    context_dir = prompt_dir / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    json_path = context_dir / "github-context.json"
    md_path = context_dir / "github-context.md"
    json_path.write_text(
        json.dumps({"events": events}, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    md_path.write_text(_event_context_markdown(events), encoding="utf-8")
    return {
        "json_path": str(json_path),
        "md_path": str(md_path),
        "event_count": len(events),
        "event_fingerprints": [
            event.get("event_fingerprint")
            for event in events
            if event.get("event_fingerprint")
        ],
    }


def _insert_file_artifact(conn, task_id, attempt_id, artifact_type, path, run_now):
    path_obj = Path(path)
    bytes_size = path_obj.stat().st_size if path_obj.exists() else None
    conn.execute(
        """
        INSERT INTO artifacts(
          artifact_id, task_id, attempt_id, artifact_type, path, bytes, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _id("artifact"),
            task_id,
            attempt_id,
            artifact_type,
            str(path_obj),
            bytes_size,
            run_now,
        ),
    )


def _upsert_route_decision(conn, task_id, route_result, run_now):
    required_skills = route_result.get("required_skills", [])
    recommended_skills = route_result.get("recommended_skills", [])
    row = conn.execute(
        "SELECT route_decision_id FROM route_decisions WHERE task_id = ? LIMIT 1",
        (task_id,),
    ).fetchone()
    if row:
        conn.execute(
            """
            UPDATE route_decisions
            SET route_id = ?, expected_output = ?, allowed_github_actions_json = ?,
                required_skills_json = ?, recommended_skills_json = ?, confidence = ?, created_at = ?
            WHERE route_decision_id = ?
            """,
            (
                route_result["route_id"],
                route_result["expected_output"],
                json.dumps(route_result["allowed_github_actions"], sort_keys=True),
                json.dumps(required_skills, sort_keys=True),
                json.dumps(recommended_skills, sort_keys=True),
                route_result["confidence"],
                run_now,
                row[0],
            ),
        )
        return
    conn.execute(
        """
        INSERT INTO route_decisions(
          route_decision_id, task_id, route_id, expected_output,
          allowed_github_actions_json, required_skills_json, recommended_skills_json,
          confidence, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _id("route"),
            task_id,
            route_result["route_id"],
            route_result["expected_output"],
            json.dumps(route_result["allowed_github_actions"], sort_keys=True),
            json.dumps(required_skills, sort_keys=True),
            json.dumps(recommended_skills, sort_keys=True),
            route_result["confidence"],
            run_now,
        ),
    )


def _refresh_prepared_task(
    conn,
    data_dir,
    db_path,
    repo,
    event,
    run_now,
    dry_run,
):
    state = _active_task_state(conn, event["workstream_id"])
    if not state:
        return None
    if state["task_lifecycle"] != "queued" or state["attempt_status"] != "prepared":
        return None
    if event.get("source_type") != "pull_request" or not event.get("has_open_dd_pr"):
        return None
    if state["route_id"] not in {"new-pr", "update-existing-pr"}:
        return None

    route_result = route.route_task(event)
    if route_result["route_id"] != "update-existing-pr":
        return None
    event_known = _task_has_event(conn, state["task_id"], event["event_fingerprint"])
    if (
        event_known
        and state["route_id"] == route_result["route_id"]
        and state["expected_output"] == route_result["expected_output"]
    ):
        return None

    worktree_result = _prepare_worktree(repo, event, route_result, dry_run=dry_run)
    related_events = _task_related_events(conn, state["task_id"])
    repo_id = _repo_id(repo["full_name"])
    runtime_knowledge_items = _load_runtime_knowledge(
        conn,
        repo_id,
        event["workstream_id"],
        route_result,
        related_events,
    )
    prompt_path = _prompt_path_for_task(conn, data_dir, state["task_id"])
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    github_context = _write_github_context_artifacts(prompt_path.parent, related_events)
    runtime_context = _runtime_context(db_path, repo, worktree_result)
    runtime_context["github_context"] = github_context
    prompt_path.write_text(
        render_prompt.render_prompt(
            {
                "task_id": state["task_id"],
                "attempt_id": state["attempt_id"],
                "workstream_id": event["workstream_id"],
            },
            route_result,
            related_events,
            runtime_context=runtime_context,
            runtime_knowledge=runtime_knowledge_items,
            project_memories=_retrieve_project_memories(
                conn,
                repo_id,
                event["workstream_id"],
                route_result,
                related_events,
                runtime_knowledge_items,
            ),
        ),
        encoding="utf-8",
    )
    for artifact_type, path in [
        ("github_context_json", github_context["json_path"]),
        ("github_context_md", github_context["md_path"]),
    ]:
        _insert_file_artifact(conn, state["task_id"], state["attempt_id"], artifact_type, path, run_now)
    conn.execute(
        """
        UPDATE tasks
        SET route_id = ?, expected_output = ?, updated_at = ?
        WHERE task_id = ?
        """,
        (
            route_result["route_id"],
            route_result["expected_output"],
            run_now,
            state["task_id"],
        ),
    )
    _upsert_route_decision(conn, state["task_id"], route_result, run_now)
    conn.execute(
        """
        UPDATE attempts
        SET worktree_path = ?, branch_name = ?
        WHERE attempt_id = ?
        """,
        (
            (worktree_result or {}).get("worktree_path"),
            (worktree_result or {}).get("branch_name"),
            state["attempt_id"],
        ),
    )
    return prompt_path


def _insert_source_and_event(conn, repo_id, event, run_now):
    source_id = _source_id(event["source_key"])
    conn.execute(
        """
        INSERT INTO github_sources(
          source_id, repo_id, source_key, source_type, number, html_url,
          title, state, author_login, source_updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
          html_url = COALESCE(excluded.html_url, github_sources.html_url),
          title = excluded.title,
          state = excluded.state,
          author_login = excluded.author_login,
          source_updated_at = COALESCE(excluded.source_updated_at, github_sources.source_updated_at)
        """,
        (
            source_id,
            repo_id,
            event["source_key"],
            event["source_type"],
            event["number"],
            event.get("url"),
            event.get("title", ""),
            event.get("state", "open"),
            event.get("actor_login"),
            event.get("source_updated_at"),
        ),
    )
    event_id = _event_id(event["event_fingerprint"])
    conn.execute(
        """
        INSERT INTO github_events(
          event_id, repo_id, source_id, event_fingerprint, event_type, actor_login,
          author_association, authorization_status, event_at, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_fingerprint) DO UPDATE SET
          authorization_status = CASE
            WHEN github_events.authorization_status = 'authorized_trigger'
              THEN github_events.authorization_status
            ELSE excluded.authorization_status
          END,
          author_association = CASE
            WHEN github_events.authorization_status = 'authorized_trigger'
              AND excluded.authorization_status != 'authorized_trigger'
              THEN github_events.author_association
            ELSE excluded.author_association
          END,
          payload_json = CASE
            WHEN github_events.authorization_status = 'authorized_trigger'
              AND excluded.authorization_status != 'authorized_trigger'
              THEN github_events.payload_json
            ELSE excluded.payload_json
          END
        """,
        (
            event_id,
            repo_id,
            source_id,
            event["event_fingerprint"],
            event["event_type"],
            event.get("actor_login"),
            event.get("author_association"),
            event.get("authorization_status", "pending"),
            event.get("event_at", run_now),
            json.dumps(event, ensure_ascii=False, sort_keys=True),
        ),
    )
    return source_id, event_id


def _append_event_to_active_task(conn, event, relationship, run_now):
    task_id = _active_task_id(conn, event["workstream_id"])
    if not task_id:
        return False
    conn.execute(
        """
        INSERT OR IGNORE INTO task_events(task_id, event_id, relationship, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (task_id, _event_id(event["event_fingerprint"]), relationship, run_now),
    )
    conn.execute(
        """
        UPDATE workstreams
        SET updated_at = ?
        WHERE workstream_id = ?
        """,
        (run_now, event["workstream_id"]),
    )
    return True


def _runtime_context(db_path, repo, worktree_result=None):
    worktree_result = worktree_result or {}
    return {
        "db_path": str(db_path),
        "python_bin": repo.get("python_bin") or sys.executable or "python3",
        "result_script": str(_worker_result_script()),
        "snapshot_script": str(_worker_snapshot_script()),
        "heartbeat_script": str(_worker_heartbeat_script()),
        "status_script": str(_agent_status_script()),
        "worktree_path": worktree_result.get("worktree_path", ""),
        "branch_name": worktree_result.get("branch_name", ""),
        "target_base_branch": worktree_result.get("base_branch") or repo["default_base_branch"],
    }


def _load_runtime_knowledge(conn, repo_id, workstream_id, route_result, events):
    try:
        return runtime_knowledge.load_runtime_knowledge(
            conn,
            repo_id=repo_id,
            route_result=route_result,
            events=events,
            workstream_id=workstream_id,
        )
    except (sqlite3.Error, TypeError, ValueError):
        return []


def _retrieve_project_memories(conn, repo_id, workstream_id, route_result, events, runtime_knowledge_items=None):
    try:
        return project_memory.retrieve_memories(
            conn,
            repo_id=repo_id,
            workstream_id=workstream_id,
            route_result=route_result,
            events=events,
            runtime_knowledge=runtime_knowledge_items,
        )
    except (sqlite3.Error, TypeError, ValueError):
        return []


def _prepare_worktree(repo, event, route_result, dry_run):
    skill_status = route_result.get("skill_status")
    if skill_status and not skill_status.get("runnable", True):
        return None
    workspace_mode = route_result.get("workspace_mode")
    if not workspace_mode:
        workspace_mode = (
            "review_pr"
            if route_result.get("worktree_mode") == "review_pr"
            else (
                "new_branch"
                if route_result.get("needs_worktree")
                else "none"
            )
        )
    if workspace_mode == "none":
        return None
    if (
        workspace_mode == "analysis"
        and not route_result.get("enforce_workspace_policy", False)
    ):
        return None
    return worktree.resolve_task_workspace(
        repo,
        {**route_result, "workspace_mode": workspace_mode},
        {
            "number": event["number"],
            "branch_slug": (
                event.get("branch_slug")
                or event.get("title")
                or "task"
            ),
            "head_branch": event.get("existing_pr_head_branch"),
        },
        dry_run,
    )


def _effective_route(config_result, repo, event):
    base_route = route.route_task(event)
    policy = ROUTE_POLICIES[base_route["route_id"]]
    resolved = route_config.resolve_route_config(
        config_result,
        repo,
        policy,
    )
    installed = skills.discover_skill_names(
        config_result["skills"]["search_paths"]
    )
    skill_status = skills.route_skill_status(
        required=resolved["required_skills"],
        recommended=resolved["recommended_skills"],
        installed=installed,
    )
    return {
        **resolved,
        "route_id": resolved["id"],
        "confidence": base_route.get("confidence", "low"),
        "needs_worktree": base_route.get("needs_worktree", False),
        "enforce_workspace_policy": config_result.get("version") == 1,
        "skill_status": skill_status,
    }


def _prepare_detected_local_tasks(
    conn,
    data_dir,
    db_path,
    repo,
    repo_id,
    run_id,
    run_now,
    dry_run,
    dispatch_queue,
    dispatch_budget,
    prompt_paths,
):
    rows = conn.execute(
        """
        SELECT
          t.task_id, t.workstream_id, t.parent_task_id, t.priority,
          t.routing_mode, t.requested_worker, t.metadata_json,
          wi.work_item_id, wi.title, wi.description, wi.created_by
        FROM tasks t
        JOIN work_items wi ON wi.workstream_id = t.workstream_id
        JOIN workstreams w ON w.workstream_id = t.workstream_id
        WHERE w.repo_id = ?
          AND wi.origin_type = 'web'
          AND t.lifecycle = 'detected'
          AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.task_id = t.task_id)
        ORDER BY t.created_at, t.task_id
        """,
        (repo_id,),
    ).fetchall()
    prepared = 0
    for row in rows:
        (
            task_id,
            workstream_id,
            parent_task_id,
            priority,
            routing_mode,
            requested_worker,
            task_metadata_json,
            work_item_id,
            title,
            description,
            created_by,
        ) = row
        event_rows = conn.execute(
            """
            SELECT event_id, event_type, actor_kind, actor_identity, body,
                   resolves_event_id, created_at, metadata_json
            FROM work_item_events
            WHERE work_item_id = ?
            ORDER BY created_at, event_id
            """,
            (work_item_id,),
        ).fetchall()
        local_events = [
            {
                "work_item_event_id": event_row[0],
                "event_type": event_row[1],
                "actor_kind": event_row[2],
                "actor_identity": event_row[3],
                "body": event_row[4],
                "resolves_event_id": event_row[5],
                "event_at": event_row[6],
                "metadata": _json_object(event_row[7]),
            }
            for event_row in event_rows
        ]
        event_ids = [event["work_item_event_id"] for event in local_events]
        normalized = {
            "origin_type": "web",
            "intent": discover.infer_intent(title, description, "manual_operator_request"),
            "event_type": "manual_operator_request",
            "title": title,
            "body": description,
        }
        route_result = route.route_task(normalized)
        source_reference = work_item_id.removeprefix("wi-")[:12]
        worktree_event = {
            "number": source_reference,
            "title": title,
            "base_branch": repo["default_base_branch"],
        }
        worktree_result = _prepare_worktree(repo, worktree_event, route_result, dry_run)
        attempt_id = _id("attempt")
        prompt_dir = data_dir / "artifacts" / task_id
        prompt_dir.mkdir(parents=True, exist_ok=True)
        context_path = prompt_dir / f"{attempt_id}.task-context.json"
        context_payload = {
            "origin_type": "web",
            "work_item_id": work_item_id,
            "repo_full_name": repo["full_name"],
            "title": title,
            "description": description,
            "created_by": created_by,
            "events": local_events,
        }
        context_path.write_text(
            json.dumps(context_payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        prompt_path = prompt_dir / f"{attempt_id}.prompt.md"
        runtime_context = _runtime_context(db_path, repo, worktree_result)
        runtime_context.update(
            {
                "origin_type": "web",
                "repo_full_name": repo["full_name"],
                "work_item_id": work_item_id,
                "work_item_event_ids": event_ids,
                "requirement": {"title": title, "description": description},
                "prior_question_summaries": [
                    event["body"]
                    for event in local_events
                    if event["event_type"] in {"operator_question", "agent_question"}
                ],
                "prior_result_summaries": [
                    event["body"]
                    for event in local_events
                    if event["event_type"] in {"result_recorded", "completed"}
                ],
                "operator_reply": next(
                    (
                        event["body"]
                        for event in reversed(local_events)
                        if event["event_type"] in {"user_response", "approved", "changes_requested"}
                    ),
                    "",
                ),
            }
        )
        prompt_path.write_text(
            render_prompt.render_prompt(
                {
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "workstream_id": workstream_id,
                },
                route_result,
                local_events,
                runtime_context=runtime_context,
            ),
            encoding="utf-8",
        )
        _upsert_route_decision(conn, task_id, route_result, run_now)
        task_metadata = _json_object(task_metadata_json)
        task_metadata["local_source_reference"] = source_reference
        conn.execute(
            """
            UPDATE tasks
            SET lifecycle = 'queued', route_id = ?, expected_output = ?,
                priority = ?, routing_mode = ?, requested_worker = ?,
                updated_at = ?, metadata_json = ?
            WHERE task_id = ?
            """,
            (
                route_result["route_id"],
                route_result["expected_output"],
                priority,
                routing_mode,
                requested_worker,
                run_now,
                json.dumps(task_metadata, ensure_ascii=False, sort_keys=True),
                task_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO attempts(
              attempt_id, task_id, attempt_no, status, worktree_path, branch_name,
              started_at, heartbeat_at, metadata_json
            ) VALUES (?, ?, 1, 'prepared', ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                task_id,
                (worktree_result or {}).get("worktree_path"),
                (worktree_result or {}).get("branch_name"),
                run_now,
                run_now,
                json.dumps({"origin_type": "web", "work_item_id": work_item_id}),
            ),
        )
        _insert_file_artifact(conn, task_id, attempt_id, "task_context", context_path, run_now)
        _insert_file_artifact(conn, task_id, attempt_id, "prompt", prompt_path, run_now)
        conn.execute(
            """
            UPDATE wakeups
            SET status = 'consumed', consumed_run_id = ?, updated_at = ?
            WHERE work_item_id = ?
              AND task_id = ?
              AND reason = 'manual_operator_request'
              AND status = 'pending'
            """,
            (run_id, run_now, work_item_id, task_id),
        )
        task_info = {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "workstream_id": workstream_id,
            "prompt_path": prompt_path,
            "route_id": route_result["route_id"],
            "routing_mode": routing_mode,
            "requested_worker": requested_worker,
        }
        _queue_dispatch(
            dispatch_queue,
            task_info,
            worktree_result,
            dispatch_budget,
            repo_id=repo_id,
            repo=repo,
        )
        prompt_paths.append(str(prompt_path))
        prepared += 1
    return prepared


def _create_task_attempt_and_prompt(
    conn,
    data_dir,
    db_path,
    repo,
    repo_id,
    event,
    route_result,
    stream,
    run_now,
    worktree_result=None,
    parent_task_id=None,
    related_events=None,
):
    skill_status = route_result.get("skill_status") or {
        "runnable": True,
        "missing_required": [],
        "missing_recommended": [],
    }
    blocked = not skill_status["runnable"]
    lifecycle = "failed" if blocked else "active"
    task_lifecycle = "failed" if blocked else "queued"
    attempt_status = "failed" if blocked else "prepared"
    related_events = list(related_events or [event])
    workstream_id = stream["workstream_id"]
    source_id = _source_id(event["source_key"])
    task_id = _id("task")
    attempt_id = _id("attempt")
    prompt_dir = data_dir / "artifacts" / task_id
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompt_dir / "prompt.md"
    task = {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "workstream_id": workstream_id,
    }
    runtime_knowledge_items = _load_runtime_knowledge(
        conn,
        repo_id,
        workstream_id,
        route_result,
        related_events,
    )
    project_memories = _retrieve_project_memories(
        conn,
        repo_id,
        workstream_id,
        route_result,
        related_events,
        runtime_knowledge_items,
    )
    github_context = _write_github_context_artifacts(prompt_dir, related_events)
    runtime_context = _runtime_context(db_path, repo, worktree_result)
    runtime_context["github_context"] = github_context
    prompt_path.write_text(
        render_prompt.render_prompt(
            task,
            route_result,
            related_events,
            runtime_context=runtime_context,
            runtime_knowledge=runtime_knowledge_items,
            project_memories=project_memories,
        ),
        encoding="utf-8",
    )

    conn.execute(
        """
        INSERT INTO workstreams(
          workstream_id, repo_id, primary_source_id, lifecycle, active_task_id,
          created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(workstream_id) DO UPDATE SET
          lifecycle = excluded.lifecycle,
          active_task_id = excluded.active_task_id,
          updated_at = excluded.updated_at
        """,
        (
            workstream_id,
            repo_id,
            source_id,
            lifecycle,
            task_id,
            run_now,
            run_now,
        ),
    )
    work_items.ensure_github_work_item(
        conn,
        repo_id=repo_id,
        source_id=source_id,
        workstream_id=workstream_id,
        actor_identity=event.get("actor_login") or "github",
        route_confidence=route_result.get("confidence", "low"),
        now=run_now,
    )
    _record_review_participant(conn, workstream_id, event, route_result, run_now)
    for related_event in related_events:
        related_source_id = _source_id(related_event["source_key"])
        conn.execute(
            """
            INSERT INTO workstream_sources(workstream_id, source_id, relationship, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(workstream_id, source_id) DO NOTHING
            """,
            (
                workstream_id,
                related_source_id,
                workstream.source_relationship(related_event),
                run_now,
            ),
        )
    conn.execute(
        """
        INSERT INTO tasks(
          task_id, workstream_id, lifecycle, parent_task_id, priority, route_id, expected_output,
          created_at, updated_at
        )
        VALUES (?, ?, ?, ?, 'P1', ?, ?, ?, ?)
        """,
        (
            task_id,
            workstream_id,
            task_lifecycle,
            parent_task_id,
            route_result["route_id"],
            route_result["expected_output"],
            run_now,
            run_now,
        ),
    )
    for index, related_event in enumerate(related_events):
        relationship = "trigger" if index == 0 else "context"
        conn.execute(
            """
            INSERT INTO task_events(task_id, event_id, relationship, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                task_id,
                _event_id(related_event["event_fingerprint"]),
                relationship,
                run_now,
            ),
        )
    conn.execute(
        """
        INSERT INTO route_decisions(
          route_decision_id, task_id, route_id, expected_output,
          allowed_github_actions_json, required_skills_json, recommended_skills_json,
          confidence, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _id("route"),
            task_id,
            route_result["route_id"],
            route_result["expected_output"],
            json.dumps(route_result["allowed_github_actions"], sort_keys=True),
            json.dumps(route_result.get("required_skills", []), sort_keys=True),
            json.dumps(route_result.get("recommended_skills", []), sort_keys=True),
            route_result["confidence"],
            run_now,
        ),
    )
    conn.execute(
        """
        INSERT INTO attempts(
          attempt_id, task_id, attempt_no, status, worktree_path, branch_name, started_at, heartbeat_at
        )
        VALUES (?, ?, 1, ?, ?, ?, ?, ?)
        """,
        (
            attempt_id,
            task_id,
            attempt_status,
            (worktree_result or {}).get("worktree_path"),
            (worktree_result or {}).get("branch_name"),
            run_now,
            run_now,
        ),
    )
    for artifact_type, path in [
        ("prompt", prompt_path),
        ("github_context_json", github_context["json_path"]),
        ("github_context_md", github_context["md_path"]),
    ]:
        _insert_file_artifact(conn, task_id, attempt_id, artifact_type, path, run_now)
    if blocked:
        _insert_notification(
            conn,
            "required_route_skills_missing",
            "recorded",
            run_now,
            metadata={
                "task_id": task_id,
                "safe_error": (
                    "missing required route skills: "
                    + ", ".join(skill_status["missing_required"])
                ),
            },
        )
    return {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "workstream_id": workstream_id,
        "prompt_path": prompt_path,
        "route_id": route_result["route_id"],
        "blocked": blocked,
    }


def _latest_route_result_for_task(conn, task_id):
    row = conn.execute(
        """
        SELECT route_id, expected_output, allowed_github_actions_json,
               required_skills_json, recommended_skills_json, confidence
        FROM route_decisions
        WHERE task_id = ?
        ORDER BY created_at DESC, route_decision_id DESC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if not row:
        return None
    (
        route_id,
        expected_output,
        allowed_actions_json,
        required_skills_json,
        recommended_skills_json,
        confidence,
    ) = row
    return {
        "route_id": route_id,
        "expected_output": expected_output,
        "allowed_github_actions": _json_list(allowed_actions_json),
        "required_skills": _json_list(required_skills_json),
        "recommended_skills": _json_list(recommended_skills_json),
        "verification_policy": route.verification_policy_for(route_id),
        "confidence": confidence,
        "needs_worktree": bool((route.ROUTES.get(route_id) or {}).get("needs_worktree")),
    }


def _events_for_task(conn, task_id):
    rows = conn.execute(
        """
        SELECT te.relationship, ge.payload_json
        FROM task_events te
        JOIN github_events ge ON ge.event_id = te.event_id
        WHERE te.task_id = ?
        ORDER BY CASE te.relationship
            WHEN 'trigger' THEN 0
            WHEN 'context' THEN 1
            WHEN 'pending' THEN 2
            ELSE 3
          END,
          ge.event_at,
          ge.event_id
        """,
        (task_id,),
    ).fetchall()
    return [json.loads(payload) for _relationship, payload in rows]


def _create_resume_attempt_and_prompt(
    conn,
    data_dir,
    db_path,
    repo,
    repo_id,
    task_id,
    workstream_id,
    previous_attempt_id,
    run_now,
    recovery_context,
):
    previous = conn.execute(
        """
        SELECT attempt_no, worktree_path, branch_name
        FROM attempts
        WHERE attempt_id = ?
        """,
        (previous_attempt_id,),
    ).fetchone()
    if not previous:
        return None
    max_attempt_no = conn.execute(
        "SELECT COALESCE(MAX(attempt_no), 0) FROM attempts WHERE task_id = ?",
        (task_id,),
    ).fetchone()[0]
    attempt_id = _id("attempt")
    attempt_no = max_attempt_no + 1
    _previous_attempt_no, worktree_path, branch_name = previous
    route_result = _latest_route_result_for_task(conn, task_id)
    events = _events_for_task(conn, task_id)
    if not route_result or not events:
        return None

    prompt_dir = data_dir / "artifacts" / task_id
    prompt_dir.mkdir(parents=True, exist_ok=True)
    recovery_path = prompt_dir / f"{attempt_id}.recovery.json"
    recovery_context = {
        **recovery_context,
        "strategy": "resume_from_worktree",
        "new_attempt_id": attempt_id,
        "recovery_artifact_path": str(recovery_path),
    }
    recovery_path.write_text(
        json.dumps(recovery_context, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    task = {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "workstream_id": workstream_id,
    }
    worktree_result = {
        "worktree_path": worktree_path or "",
        "branch_name": branch_name or "",
    }
    runtime_knowledge_items = _load_runtime_knowledge(
        conn,
        repo_id,
        workstream_id,
        route_result,
        events,
    )
    project_memories = _retrieve_project_memories(
        conn,
        repo_id,
        workstream_id,
        route_result,
        events,
        runtime_knowledge_items,
    )
    runtime_context = _runtime_context(db_path, repo, worktree_result)
    runtime_context["recovery_context"] = recovery_context
    prompt_path = prompt_dir / f"{attempt_id}.prompt.md"
    github_context = _write_github_context_artifacts(prompt_dir, events)
    runtime_context["github_context"] = github_context
    prompt_path.write_text(
        render_prompt.render_prompt(
            task,
            route_result,
            events,
            runtime_context=runtime_context,
            runtime_knowledge=runtime_knowledge_items,
            project_memories=project_memories,
        ),
        encoding="utf-8",
    )

    conn.execute(
        """
        INSERT INTO attempts(
          attempt_id, task_id, attempt_no, status, worktree_path, branch_name,
          started_at, heartbeat_at, metadata_json
        )
        VALUES (?, ?, ?, 'prepared', ?, ?, ?, ?, ?)
        """,
        (
            attempt_id,
            task_id,
            attempt_no,
            worktree_path,
            branch_name,
            run_now,
            run_now,
            json.dumps(
                {
                    "resume": {
                        "previous_attempt_id": previous_attempt_id,
                        "strategy": "resume_from_worktree",
                        "recovery_artifact_path": str(recovery_path),
                    }
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        ),
    )
    for artifact_type, path in [
        ("recovery_context", recovery_path),
        ("prompt", prompt_path),
        ("github_context_json", github_context["json_path"]),
        ("github_context_md", github_context["md_path"]),
    ]:
        _insert_file_artifact(conn, task_id, attempt_id, artifact_type, path, run_now)
    conn.execute(
        "UPDATE tasks SET lifecycle = 'running', updated_at = ? WHERE task_id = ?",
        (run_now, task_id),
    )
    conn.execute(
        """
        UPDATE workstreams
        SET lifecycle = 'active', active_task_id = ?, updated_at = ?
        WHERE workstream_id = ?
        """,
        (task_id, run_now, workstream_id),
    )
    return {
        "attempt_id": attempt_id,
        "attempt_no": attempt_no,
        "prompt_path": str(prompt_path),
        "recovery_artifact_path": str(recovery_path),
    }


def _record_dispatch_result(conn, attempt_id, dispatch_result):
    status = dispatch_result.get("status", "prepared")
    if status not in {"prepared", "running", "completed", "failed", "stale", "canceled"}:
        status = "failed" if not dispatch_result.get("ok", True) else "prepared"
    current = conn.execute(
        "SELECT status, metadata_json FROM attempts WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    if current and current[0] in {"completed", "failed", "canceled"}:
        metadata = _json_object(current[1])
        metadata["dispatch"] = dispatch_result
        conn.execute(
            """
            UPDATE attempts
            SET metadata_json = ?
            WHERE attempt_id = ?
            """,
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True), attempt_id),
        )
        return
    metadata_obj = _json_object(current[1] if current else None)
    metadata_obj["dispatch"] = dispatch_result
    metadata = json.dumps(metadata_obj, ensure_ascii=False, sort_keys=True)
    if status == "running":
        started_at = _now()
        conn.execute(
            """
            UPDATE attempts
            SET status = ?, started_at = ?, heartbeat_at = ?, finished_at = NULL,
                metadata_json = ?
            WHERE attempt_id = ?
            """,
            (
                status,
                started_at,
                started_at,
                metadata,
                attempt_id,
            ),
        )
        return
    if status == "failed":
        finished_at = _now()
        failure_json = json.dumps(dispatch_result, ensure_ascii=False, sort_keys=True)
        conn.execute(
            """
            UPDATE attempts
            SET status = ?, finished_at = ?, failure_json = ?, metadata_json = ?
            WHERE attempt_id = ?
            """,
            (
                status,
                finished_at,
                failure_json,
                metadata,
                attempt_id,
            ),
        )
        return
    conn.execute(
        """
        UPDATE attempts
        SET status = ?, metadata_json = ?
        WHERE attempt_id = ?
        """,
        (
            status,
            metadata,
            attempt_id,
        ),
    )


def _record_dispatch_artifacts(conn, task_info, dispatch_result, run_now):
    for key, artifact_type in [
        ("stdout_path", "worker_stdout"),
        ("stderr_path", "worker_stderr"),
    ]:
        path = dispatch_result.get(key)
        if not path:
            continue
        path_obj = Path(path)
        bytes_size = path_obj.stat().st_size if path_obj.exists() else None
        conn.execute(
            """
            INSERT INTO artifacts(
              artifact_id, task_id, attempt_id, artifact_type, path, bytes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _id("artifact"),
                task_info["task_id"],
                task_info["attempt_id"],
                artifact_type,
                str(path_obj),
                bytes_size,
                run_now,
            ),
        )


def _completed_by_worker_result(conn, attempt_id):
    row = conn.execute(
        """
        SELECT 1
        FROM attempts a
        JOIN worker_results wr ON wr.attempt_id = a.attempt_id
        WHERE a.attempt_id = ?
          AND a.status = 'completed'
        LIMIT 1
        """,
        (attempt_id,),
    ).fetchone()
    return bool(row)


def _queue_dispatch(
    dispatch_queue,
    task_info,
    worktree_result,
    dispatch_budget=None,
    repo_id=None,
    repo=None,
):
    if task_info.get("blocked"):
        return False
    if dispatch_budget is not None:
        if dispatch_budget["remaining"] <= 0:
            return False
        dispatch_budget["remaining"] -= 1
    dispatch_queue.append(
        {
            "task_info": task_info,
            "worktree_result": worktree_result,
            "repo_id": repo_id,
            "repo": repo,
        }
    )
    return True


def _queue_prepared_attempts(conn, repo_id, repo, dispatch_queue, capacity):
    if capacity <= 0:
        return 0
    rows = conn.execute(
        """
        SELECT a.attempt_id, a.task_id, t.workstream_id, t.route_id, a.worktree_path,
               ar.path, t.routing_mode, t.requested_worker
        FROM attempts a
        JOIN tasks t ON t.task_id = a.task_id
        JOIN workstreams w ON w.workstream_id = t.workstream_id
        JOIN artifacts ar ON ar.task_id = t.task_id
        WHERE w.repo_id = ?
          AND a.status = 'prepared'
          AND t.lifecycle IN ('queued', 'running')
          AND ar.artifact_type = 'prompt'
        ORDER BY
          (CASE t.priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END)
          - MIN(3, CAST(MAX(0, julianday('now') - julianday(t.created_at)) AS INTEGER)),
          CASE
            WHEN t.route_id = 'update-existing-pr' THEN 0
            WHEN t.workstream_id LIKE 'github:%!%' THEN 1
            ELSE 2
          END,
          a.started_at,
          a.attempt_id
        LIMIT ?
        """,
        (repo_id, capacity),
    ).fetchall()
    for (
        attempt_id,
        task_id,
        workstream_id,
        route_id,
        worktree_path,
        prompt_path,
        routing_mode,
        requested_worker,
    ) in rows:
        task_info = {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "workstream_id": workstream_id,
            "route_id": route_id,
            "prompt_path": Path(prompt_path),
            "routing_mode": routing_mode,
            "requested_worker": requested_worker,
        }
        worktree_result = {"worktree_path": worktree_path} if worktree_path else None
        _queue_dispatch(
            dispatch_queue,
            task_info,
            worktree_result,
            repo_id=repo_id,
            repo=repo,
        )
    return len(rows)


def _current_dispatch_target(conn, attempt_id):
    row = conn.execute(
        """
        SELECT a.task_id, t.workstream_id, t.route_id, a.worktree_path, ar.path,
               t.routing_mode, t.requested_worker
        FROM attempts a
        JOIN tasks t ON t.task_id = a.task_id
        JOIN artifacts ar ON ar.task_id = t.task_id
        WHERE a.attempt_id = ?
          AND a.status = 'prepared'
          AND t.lifecycle IN ('queued', 'running')
          AND ar.artifact_type = 'prompt'
        ORDER BY ar.created_at DESC, ar.artifact_id DESC
        LIMIT 1
        """,
        (attempt_id,),
    ).fetchone()
    if not row:
        return None
    (
        task_id,
        workstream_id,
        route_id,
        worktree_path,
        prompt_path,
        routing_mode,
        requested_worker,
    ) = row
    task_info = {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "workstream_id": workstream_id,
        "route_id": route_id,
        "prompt_path": Path(prompt_path),
        "routing_mode": routing_mode,
        "requested_worker": requested_worker,
    }
    worktree_result = {"worktree_path": worktree_path} if worktree_path else None
    return task_info, worktree_result


def _prepared_attempt_event_and_route(conn, attempt_id):
    row = conn.execute(
        """
        SELECT ge.payload_json, rd.route_id
        FROM attempts a
        JOIN task_events te ON te.task_id = a.task_id
        JOIN github_events ge ON ge.event_id = te.event_id
        JOIN route_decisions rd ON rd.task_id = a.task_id
        WHERE a.attempt_id = ?
        ORDER BY CASE te.relationship WHEN 'trigger' THEN 0 ELSE 1 END,
                 te.created_at,
                 rd.created_at DESC
        LIMIT 1
        """,
        (attempt_id,),
    ).fetchone()
    if row:
        event = json.loads(row[0])
        route_id = row[1]
        route_result = dict(route.ROUTES.get(route_id) or route.route_task(event))
        return event, route_result
    row = conn.execute(
        """
        SELECT wi.work_item_id, wi.title, wi.description, a.branch_name, rd.route_id,
               t.metadata_json
        FROM attempts a
        JOIN tasks t ON t.task_id = a.task_id
        JOIN work_items wi ON wi.workstream_id = t.workstream_id
        JOIN route_decisions rd ON rd.task_id = t.task_id
        WHERE a.attempt_id = ? AND wi.origin_type = 'web'
        ORDER BY rd.created_at DESC
        LIMIT 1
        """,
        (attempt_id,),
    ).fetchone()
    if not row:
        return None, None
    task_metadata = _json_object(row[5])
    event = {
        "origin_type": "web",
        "number": task_metadata.get("local_source_reference")
        or row[0].removeprefix("wi-")[:12],
        "title": row[1],
        "body": row[2],
        "existing_pr_head_branch": row[3],
    }
    route_id = row[4]
    route_result = dict(route.ROUTES.get(route_id) or route.route_task(event))
    return event, route_result


def _can_materialize_worktree(repo):
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo["repo_root"],
            text=True,
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return completed.stdout.strip() == "true"


def _ensure_dispatch_worktree(db_path, repo, task_info, worktree_result):
    worktree_path = (worktree_result or {}).get("worktree_path")
    if not worktree_path or Path(worktree_path).exists():
        return worktree_result
    if not _can_materialize_worktree(repo):
        return worktree_result

    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        event, route_result = _prepared_attempt_event_and_route(conn, task_info["attempt_id"])
    if not event or not route_result.get("needs_worktree"):
        return worktree_result

    refreshed = _prepare_worktree(repo, event, route_result, dry_run=False)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            """
            UPDATE attempts
            SET worktree_path = ?, branch_name = ?
            WHERE attempt_id = ?
            """,
            (
                refreshed.get("worktree_path"),
                refreshed.get("branch_name"),
                task_info["attempt_id"],
            ),
        )
    return refreshed


def _dispatch_queued(
    db_path,
    repo_ids,
    dispatch_queue,
    dry_run,
    max_concurrency,
    workers=None,
    default_worker=None,
    route_worker_models=None,
    config_result=None,
):
    results = []
    default_worker = default_worker or (workers or [])[0]
    workers_by_name = {
        worker["name"]: worker
        for worker in (workers or [default_worker])
    }
    if not dry_run and len(dispatch_queue) > 1:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            dispatch_queue = sorted(
                dispatch_queue,
                key=lambda item: _prepared_attempt_dispatch_priority(
                    conn,
                    item["task_info"]["attempt_id"],
                ),
            )
    for item in dispatch_queue:
        task_info = item["task_info"]
        worktree_result = item["worktree_result"]
        repo_id = item["repo_id"]
        repo = item["repo"]
        if not dry_run:
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("PRAGMA busy_timeout = 5000")
                if _running_attempt_count_for_repos(conn, repo_ids) >= max_concurrency:
                    continue
                if _running_attempt_count(conn, repo_id) >= int(
                    (repo or {}).get("max_concurrency", max_concurrency)
                ):
                    continue
                current_target = _current_dispatch_target(conn, task_info["attempt_id"])
                if current_target is None:
                    continue
                task_info, worktree_result = current_target
            worktree_result = _ensure_dispatch_worktree(db_path, repo, task_info, worktree_result)
        route_worker_config = (route_worker_models or {}).get(task_info.get("route_id"))
        selected_worker = default_worker
        if task_info.get("routing_mode") == "manual":
            selected_worker = workers_by_name.get(task_info.get("requested_worker"))
            if selected_worker is None:
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.row_factory = sqlite3.Row
                    item = work_items.resolve_work_item_for_workstream(
                        conn,
                        task_info["workstream_id"],
                    )
                    if item:
                        work_items.record_system_event(
                            conn,
                            item["work_item_id"],
                            event_type="manual_worker_unavailable",
                            idempotency_key=(
                                f"manual-worker-unavailable:{task_info['task_id']}:"
                                f"{task_info.get('requested_worker') or 'missing'}"
                            ),
                            body="The selected Agent is unavailable.",
                            metadata={
                                "task_id": task_info["task_id"],
                                "requested_worker": task_info.get("requested_worker"),
                            },
                        )
                continue
        elif config_result and config_result.get("version") == 1:
            policy = ROUTE_POLICIES.get(task_info.get("route_id"))
            if policy:
                effective_route = route_config.resolve_route_config(
                    config_result,
                    repo,
                    policy,
                )
                selected_worker = workers_by_name[
                    effective_route["worker"]
                ]
        elif route_worker_config:
            selected_worker = workers_by_name[route_worker_config["worker"]]
        worker_kwargs = {
            "task_id": task_info["task_id"],
            "attempt_id": task_info["attempt_id"],
            "prompt_path": task_info["prompt_path"],
            "worktree_path": (worktree_result or {}).get("worktree_path"),
            "worker_command": selected_worker["command"],
            "worker_agent": selected_worker["agent"],
            "worker_agent_config": selected_worker,
            "model": (
                route_worker_config["model"]
                if route_worker_config and task_info.get("routing_mode") != "manual"
                else selected_worker["default_model"]
            ),
            "reasoning_effort": (
                route_worker_config["effort"]
                if route_worker_config and task_info.get("routing_mode") != "manual"
                else selected_worker["default_effort"]
            ),
            "dry_run": dry_run,
        }
        try:
            dispatch_result = dispatch.dispatch_worker(**worker_kwargs)
        except Exception as exc:
            dispatch_result = {
                **_safe_dispatch_error(exc),
                "task_id": task_info["task_id"],
                "attempt_id": task_info["attempt_id"],
            }
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            _record_dispatch_result(conn, task_info["attempt_id"], dispatch_result)
            _record_dispatch_artifacts(conn, task_info, dispatch_result, _now())
            if (
                (not dispatch_result.get("ok", True) or dispatch_result.get("status") == "failed")
                and _completed_by_worker_result(conn, task_info["attempt_id"])
            ):
                dispatch_result = {
                    **dispatch_result,
                    "ok": True,
                    "status": "completed_by_worker_result",
                    "original_status": dispatch_result.get("status"),
                }
            if not dispatch_result.get("ok", True) or dispatch_result.get("status") == "failed":
                run_now = _now()
                _finalize_failed_task(
                    conn,
                    task_info["task_id"],
                    task_info["workstream_id"],
                    run_now,
                    dispatch_result,
                )
                _insert_notification(
                    conn,
                    "worker_dispatch_failed",
                    "recorded",
                    run_now,
                    {
                        "task_id": task_info["task_id"],
                        "attempt_id": task_info["attempt_id"],
                        "workstream_id": task_info["workstream_id"],
                        "safe_error": dispatch_result.get("safe_error", ""),
                    },
                )
        results.append(dispatch_result)
    return results


def _legacy_recommended_route_from_handoff(output_type, handoff):
    if output_type != "classification_result":
        return ""
    for line in (handoff or "").splitlines():
        match = re.fullmatch(r"\s*Recommend route\s+([a-z0-9][a-z0-9-]*)\s*\.?\s*", line, re.I)
        if not match:
            continue
        recommended_route = match.group(1).lower()
        if recommended_route in route.ROUTES and recommended_route != "classification-result":
            return recommended_route
    return ""


def _load_result_payload(conn, result_row):
    (
        result_id,
        task_id,
        attempt_id,
        output_type,
        consumed_json,
        verification_json,
        handoff,
        metadata_json,
    ) = result_row
    actions = []
    for row in conn.execute(
        """
        SELECT metadata_json, target_url, external_id
        FROM github_actions
        WHERE result_id = ?
        ORDER BY created_at, action_id
        """,
        (result_id,),
    ):
        action = json.loads(row[0])
        if row[1] and not action.get("target_url"):
            action["target_url"] = row[1]
        if row[1] and not action.get("url"):
            action["url"] = row[1]
        if row[2] and not action.get("id"):
            action["id"] = row[2]
        actions.append(action)
    metadata = json.loads(metadata_json) if metadata_json else {}
    recommended_route = metadata.get("recommended_route", "")
    if not recommended_route:
        recommended_route = _legacy_recommended_route_from_handoff(output_type, handoff)
    return {
        "result_id": result_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "output_type": output_type,
        "planned_github_actions": actions,
        "consumed_event_fingerprints": json.loads(consumed_json),
        "verification": json.loads(verification_json),
        "handoff": handoff,
        "used_skills": metadata.get("used_skills", []),
        "memory_delta": metadata.get("memory_delta"),
        "used_memory_ids": metadata.get("used_memory_ids", []),
        "review_point_evaluation": metadata.get("review_point_evaluation", []),
        "recommended_route": recommended_route,
        "branch_slug": metadata.get("branch_slug", ""),
        "consumed_work_item_event_ids": metadata.get(
            "consumed_work_item_event_ids",
            [],
        ),
        "operator_question": metadata.get("operator_question"),
    }


def _set_github_actions_status(conn, result_id, status):
    conn.execute(
        """
        UPDATE github_actions
        SET audit_status = ?
        WHERE result_id = ?
        """,
        (status, result_id),
    )


def _record_result_audit(conn, result_id, audit):
    row = conn.execute(
        "SELECT metadata_json FROM worker_results WHERE result_id = ?",
        (result_id,),
    ).fetchone()
    metadata = json.loads(row[0]) if row and row[0] else {}
    metadata["audit"] = audit
    conn.execute(
        """
        UPDATE worker_results
        SET metadata_json = ?
        WHERE result_id = ?
        """,
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True), result_id),
    )


def _record_result_usage_status(conn, attempt_id, result_id):
    attempt_row = conn.execute(
        "SELECT metadata_json FROM attempts WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    attempt_metadata = _json_object(attempt_row[0] if attempt_row else None)
    usage_status = usage.extract_attempt_usage(attempt_metadata)
    if attempt_row and attempt_metadata.get("usage") != usage_status:
        attempt_metadata["usage"] = usage_status
        conn.execute(
            """
            UPDATE attempts
            SET metadata_json = ?
            WHERE attempt_id = ?
            """,
            (json.dumps(attempt_metadata, ensure_ascii=False, sort_keys=True), attempt_id),
        )
    result_row = conn.execute(
        "SELECT metadata_json FROM worker_results WHERE result_id = ?",
        (result_id,),
    ).fetchone()
    result_metadata = _json_object(result_row[0] if result_row else None)
    result_metadata["usage"] = usage_status
    conn.execute(
        """
        UPDATE worker_results
        SET metadata_json = ?
        WHERE result_id = ?
        """,
        (json.dumps(result_metadata, ensure_ascii=False, sort_keys=True), result_id),
    )
    return usage_status


def _record_result_project_memory_status(conn, result_id, status):
    row = conn.execute(
        "SELECT metadata_json FROM worker_results WHERE result_id = ?",
        (result_id,),
    ).fetchone()
    metadata = json.loads(row[0]) if row and row[0] else {}
    metadata["project_memory"] = status
    conn.execute(
        """
        UPDATE worker_results
        SET metadata_json = ?
        WHERE result_id = ?
        """,
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True), result_id),
    )


def _mark_consumed_events(conn, task_id, consumed_fingerprints):
    for fingerprint in consumed_fingerprints:
        conn.execute(
            """
            UPDATE task_events
            SET relationship = 'consumed'
            WHERE task_id = ?
              AND event_id IN (
                SELECT event_id FROM github_events WHERE event_fingerprint = ?
              )
            """,
            (task_id, fingerprint),
        )


def _pending_events_for_task(conn, task_id, consumed_fingerprints):
    consumed = set(consumed_fingerprints)
    rows = conn.execute(
        """
        SELECT ge.payload_json, ge.event_fingerprint
        FROM task_events te
        JOIN github_events ge ON ge.event_id = te.event_id
        WHERE te.task_id = ?
          AND te.relationship IN ('pending', 'context')
        ORDER BY ge.event_at, ge.event_id
        """,
        (task_id,),
    ).fetchall()
    return [json.loads(payload) for payload, fingerprint in rows if fingerprint not in consumed]


def _finalize_failed_task(conn, task_id, workstream_id, run_now, audit):
    conn.execute(
        "UPDATE tasks SET lifecycle = 'failed', updated_at = ? WHERE task_id = ?",
        (run_now, task_id),
    )
    conn.execute(
        """
        UPDATE workstreams
        SET lifecycle = 'failed', active_task_id = NULL, updated_at = ?,
            metadata_json = ?
        WHERE workstream_id = ?
        """,
        (
            run_now,
            json.dumps({"last_audit": audit}, ensure_ascii=False, sort_keys=True),
            workstream_id,
        ),
    )


def _mark_task_waiting_for_publish(conn, task_id, workstream_id, run_now):
    conn.execute(
        "UPDATE tasks SET lifecycle = 'running', updated_at = ? WHERE task_id = ?",
        (run_now, task_id),
    )
    conn.execute(
        """
        UPDATE workstreams
        SET lifecycle = 'active', active_task_id = ?, updated_at = ?
        WHERE workstream_id = ?
        """,
        (task_id, run_now, workstream_id),
    )


def _mark_task_waiting_for_user(conn, task_id, workstream_id, run_now):
    conn.execute(
        "UPDATE tasks SET lifecycle = 'waiting_for_user', updated_at = ? WHERE task_id = ?",
        (run_now, task_id),
    )
    conn.execute(
        """
        UPDATE workstreams
        SET lifecycle = 'waiting_for_user', active_task_id = ?, updated_at = ?
        WHERE workstream_id = ?
        """,
        (task_id, run_now, workstream_id),
    )


def _materialize_open_pr_workstream(conn, repo_id, repo_full_name, origin_workstream_id, action, run_now):
    match = re.search(r"/pull/(\d+)/?$", action.get("url") or "")
    if not match:
        return
    pr_number = int(match.group(1))
    pr_source_key = discover.source_key(repo_full_name, "pull_request", pr_number)
    pr_source_id = _source_id(pr_source_key)
    conn.execute(
        """
        INSERT INTO github_sources(
          source_id, repo_id, source_key, source_type, number, html_url, title, state, author_login
        )
        VALUES (?, ?, ?, 'pull_request', ?, ?, '', 'open', NULL)
        ON CONFLICT(source_key) DO UPDATE SET
          html_url = excluded.html_url,
          state = 'open'
        """,
        (pr_source_id, repo_id, pr_source_key, pr_number, action.get("url")),
    )
    conn.execute(
        """
        INSERT INTO workstreams(
          workstream_id, repo_id, primary_source_id, origin_workstream_id, lifecycle,
          active_task_id, created_at, updated_at, metadata_json
        )
        VALUES (?, ?, ?, ?, 'completed', NULL, ?, ?, '{}')
        ON CONFLICT(workstream_id) DO UPDATE SET
          origin_workstream_id = excluded.origin_workstream_id,
          primary_source_id = excluded.primary_source_id,
          updated_at = excluded.updated_at
        """,
        (pr_source_key, repo_id, pr_source_id, origin_workstream_id, run_now, run_now),
    )
    conn.execute(
        """
        INSERT INTO workstream_sources(workstream_id, source_id, relationship, created_at)
        VALUES (?, ?, 'derived_pr', ?)
        ON CONFLICT(workstream_id, source_id) DO NOTHING
        """,
        (pr_source_key, pr_source_id, run_now),
    )
    item = work_items.resolve_work_item_for_workstream(conn, pr_source_key)
    if item:
        work_items.record_system_event(
            conn,
            item["work_item_id"],
            event_type="pr_opened",
            idempotency_key=f"pr-opened:{pr_source_key}",
            metadata={
                "source_id": pr_source_id,
                "source_key": pr_source_key,
                "pr_number": pr_number,
                "url": action.get("url"),
            },
            now=run_now,
        )


def _create_child_task_for_pending_events(
    conn,
    data_dir,
    db_path,
    repo,
    repo_id,
    parent_task_id,
    workstream_id,
    pending_events,
    run_now,
    dry_run,
    dispatch_queue,
    dispatch_budget,
):
    if not pending_events:
        conn.execute(
            """
            UPDATE workstreams
            SET lifecycle = 'completed', active_task_id = NULL, updated_at = ?
            WHERE workstream_id = ?
            """,
            (run_now, workstream_id),
        )
        return None

    child_event = pending_events[0]
    stream = {
        "workstream_id": workstream_id,
        "source_relationship": workstream.source_relationship(child_event),
    }
    route_result = route.route_task(child_event)
    worktree_result = _prepare_worktree(repo, child_event, route_result, dry_run)
    task_info = _create_task_attempt_and_prompt(
        conn,
        data_dir,
        db_path,
        repo,
        repo_id,
        child_event,
        route_result,
        stream,
        run_now,
        worktree_result=worktree_result,
        parent_task_id=parent_task_id,
        related_events=pending_events,
    )
    _queue_dispatch(
        dispatch_queue,
        task_info,
        worktree_result,
        dispatch_budget,
        repo_id=repo_id,
        repo=repo,
    )
    return task_info


def _create_child_task_for_recommended_route(
    conn,
    data_dir,
    db_path,
    repo,
    repo_id,
    parent_task_id,
    workstream_id,
    related_events,
    recommended_route,
    branch_slug,
    run_now,
    dry_run,
    dispatch_queue,
    dispatch_budget,
):
    if not recommended_route:
        return None
    route_result = route.ROUTES.get(recommended_route)
    if not route_result or route_result["route_id"] == "classification-result":
        return None
    related_events = list(related_events)
    if not related_events:
        return None
    child_event = related_events[0]
    stream = {
        "workstream_id": workstream_id,
        "source_relationship": workstream.source_relationship(child_event),
    }
    worktree_event = dict(child_event)
    if branch_slug:
        worktree_event["branch_slug"] = branch_slug
    worktree_result = _prepare_worktree(repo, worktree_event, route_result, dry_run)
    task_info = _create_task_attempt_and_prompt(
        conn,
        data_dir,
        db_path,
        repo,
        repo_id,
        child_event,
        route_result,
        stream,
        run_now,
        worktree_result=worktree_result,
        parent_task_id=parent_task_id,
        related_events=related_events,
    )
    _queue_dispatch(
        dispatch_queue,
        task_info,
        worktree_result,
        dispatch_budget,
        repo_id=repo_id,
        repo=repo,
    )
    return task_info


def _result_actions_all_published(conn, result_id):
    rows = conn.execute(
        """
        SELECT audit_status, publish_status
        FROM github_actions
        WHERE result_id = ?
        """,
        (result_id,),
    ).fetchall()
    if not rows:
        return True
    return all(audit_status == "accepted" and publish_status == "published" for audit_status, publish_status in rows)


def _result_audit_accepted(conn, result_id):
    row = conn.execute(
        "SELECT metadata_json FROM worker_results WHERE result_id = ?",
        (result_id,),
    ).fetchone()
    metadata = json.loads(row[0]) if row and row[0] else {}
    return (metadata.get("audit") or {}).get("status") == "accepted"


def _finalize_accepted_result(
    conn,
    data_dir,
    db_path,
    repo,
    repo_id,
    payload,
    workstream_id,
    run_now,
    dry_run,
    dispatch_queue,
    dispatch_budget,
):
    work_item_row = conn.execute(
        "SELECT work_item_id, origin_type FROM work_items WHERE workstream_id = ?",
        (workstream_id,),
    ).fetchone()
    if payload["output_type"] == "waiting_for_user":
        _mark_consumed_events(conn, payload["task_id"], payload["consumed_event_fingerprints"])
        if work_item_row:
            question = payload.get("operator_question") or {}
            work_items.record_system_event(
                conn,
                work_item_row[0],
                event_type="operator_question",
                idempotency_key=f"result-question:{payload['result_id']}",
                body=question.get("summary", "Operator input is required."),
                metadata={
                    **question,
                    "task_id": payload["task_id"],
                    "result_id": payload["result_id"],
                },
                now=run_now,
            )
        _mark_task_waiting_for_user(conn, payload["task_id"], workstream_id, run_now)
        return
    for action in payload["planned_github_actions"]:
        if action.get("type") == "open_pr":
            _materialize_open_pr_workstream(
                conn,
                repo_id,
                repo["full_name"],
                workstream_id,
                action,
                run_now,
            )
    related_events = _task_related_events(conn, payload["task_id"])
    _mark_consumed_events(conn, payload["task_id"], payload["consumed_event_fingerprints"])
    pending_events = _pending_events_for_task(
        conn,
        payload["task_id"],
        payload["consumed_event_fingerprints"],
    )
    conn.execute(
        "UPDATE tasks SET lifecycle = 'completed', updated_at = ? WHERE task_id = ?",
        (run_now, payload["task_id"]),
    )
    if work_item_row and work_item_row[1] == "web" and payload["output_type"] == "local_result":
        work_items.record_system_event(
            conn,
            work_item_row[0],
            event_type="result_recorded",
            idempotency_key=f"result:{payload['result_id']}",
            body=payload.get("handoff", ""),
            metadata={"task_id": payload["task_id"], "result_id": payload["result_id"]},
            now=run_now,
        )
        conn.execute(
            """
            UPDATE work_items
            SET completed_at = ?, updated_at = ?, version = version + 1
            WHERE work_item_id = ? AND completed_at IS NULL
            """,
            (run_now, run_now, work_item_row[0]),
        )
    if payload["output_type"] == "classification_result":
        child_task = _create_child_task_for_recommended_route(
            conn,
            data_dir,
            db_path,
            repo,
            repo_id,
            payload["task_id"],
            workstream_id,
            related_events,
            payload.get("recommended_route"),
            payload.get("branch_slug"),
            run_now,
            dry_run,
            dispatch_queue,
            dispatch_budget,
        )
        if child_task:
            if work_item_row:
                decision = conn.execute(
                    """
                    SELECT e.event_id
                    FROM work_item_events e
                    WHERE e.work_item_id = ?
                      AND e.event_type = 'operator_decision_required'
                      AND NOT EXISTS (
                        SELECT 1 FROM work_item_events response
                        WHERE response.resolves_event_id = e.event_id
                      )
                    ORDER BY e.created_at DESC, e.event_id DESC
                    LIMIT 1
                    """,
                    (work_item_row[0],),
                ).fetchone()
                if decision:
                    work_items.record_system_event(
                        conn,
                        work_item_row[0],
                        event_type="routing_selected",
                        idempotency_key=f"classification-routing:{payload['result_id']}",
                        resolves_event_id=decision[0],
                        metadata={
                            "result_id": payload["result_id"],
                            "task_id": child_task["task_id"],
                            "route_id": child_task["route_id"],
                        },
                        now=run_now,
                    )
                    conn.execute(
                        """
                        UPDATE work_items
                        SET activated_at = COALESCE(activated_at, ?),
                            updated_at = ?,
                            version = version + 1
                        WHERE work_item_id = ?
                        """,
                        (run_now, run_now, work_item_row[0]),
                    )
            return
    _create_child_task_for_pending_events(
        conn,
        data_dir,
        db_path,
        repo,
        repo_id,
        payload["task_id"],
        workstream_id,
        pending_events,
        run_now,
        dry_run,
        dispatch_queue,
        dispatch_budget,
    )


def _audit_completed_results(
    conn,
    repo_id,
    data_dir,
    db_path,
    repo,
    run_id,
    run_now,
    dry_run,
    dispatch_queue,
    dispatch_budget,
):
    rows = conn.execute(
        """
        SELECT
          wr.result_id,
          wr.task_id,
          wr.attempt_id,
          wr.output_type,
          wr.consumed_event_fingerprints_json,
          wr.verification_json,
          wr.handoff,
          wr.metadata_json,
          t.workstream_id,
          rd.allowed_github_actions_json,
          rd.expected_output,
          rd.required_skills_json,
          rd.recommended_skills_json,
          rd.route_id
        FROM worker_results wr
        JOIN tasks t ON t.task_id = wr.task_id
        JOIN attempts a ON a.attempt_id = wr.attempt_id
        JOIN route_decisions rd ON rd.task_id = wr.task_id
        WHERE t.workstream_id IN (
          SELECT workstream_id FROM workstreams WHERE repo_id = ?
        )
          AND t.lifecycle IN ('queued', 'running')
          AND a.status = 'completed'
        ORDER BY wr.created_at, wr.result_id
        """,
        (repo_id,),
    ).fetchall()
    audited = 0
    for row in rows:
        result_row = row[:8]
        if _json_object(result_row[7]).get("audit"):
            continue
        workstream_id = row[8]
        allowed_actions = json.loads(row[9])
        expected_output = row[10]
        required_skills = json.loads(row[11] or "[]")
        recommended_skills = json.loads(row[12] or "[]")
        verification_policy = route.verification_policy_for(row[13])
        payload = _load_result_payload(conn, result_row)
        origin_row = conn.execute(
            "SELECT origin_type FROM work_items WHERE workstream_id = ?",
            (workstream_id,),
        ).fetchone()
        origin_type = origin_row[0] if origin_row else "github"
        _record_result_usage_status(conn, payload["attempt_id"], payload["result_id"])
        audit = audit_result.audit_result(
            payload,
            allowed_actions,
            recommended_skills=recommended_skills,
            required_skills=required_skills,
            expected_output=expected_output,
            verification_policy=verification_policy,
            origin_type=origin_type,
        )
        _record_result_audit(conn, payload["result_id"], audit)
        wakeup.consume_wakeups_for_results(
            conn,
            result_ids=[payload["result_id"]],
            run_id=run_id,
            now=run_now,
        )
        if audit["status"] != "accepted":
            action_status = "policy_violation" if audit["status"] == "policy_violation" else "failed"
            _set_github_actions_status(conn, payload["result_id"], action_status)
            _finalize_failed_task(conn, payload["task_id"], workstream_id, run_now, audit)
            _insert_notification(
                conn,
                "worker_result_rejected",
                "recorded",
                run_now,
                {
                    "task_id": payload["task_id"],
                    "result_id": payload["result_id"],
                    "audit": audit,
                },
            )
            audited += 1
            continue

        _set_github_actions_status(conn, payload["result_id"], "accepted")
        memory_status = project_memory.record_memory_delta(
            conn,
            payload,
            workstream_id=workstream_id,
            repo_id=repo_id,
            run_now=run_now,
        )
        _record_result_project_memory_status(conn, payload["result_id"], memory_status)
        if _result_actions_all_published(conn, payload["result_id"]):
            _finalize_accepted_result(
                conn,
                data_dir,
                db_path,
                repo,
                repo_id,
                payload,
                workstream_id,
                run_now,
                dry_run,
                dispatch_queue,
                dispatch_budget,
            )
        else:
            _mark_task_waiting_for_publish(conn, payload["task_id"], workstream_id, run_now)
        audited += 1
    return audited


def _finalize_published_results(
    conn,
    repo_id,
    data_dir,
    db_path,
    repo,
    run_id,
    run_now,
    dry_run,
    dispatch_queue,
    dispatch_budget,
):
    rows = conn.execute(
        """
        SELECT
          wr.result_id,
          wr.task_id,
          wr.attempt_id,
          wr.output_type,
          wr.consumed_event_fingerprints_json,
          wr.verification_json,
          wr.handoff,
          wr.metadata_json,
          t.workstream_id
        FROM worker_results wr
        JOIN tasks t ON t.task_id = wr.task_id
        JOIN attempts a ON a.attempt_id = wr.attempt_id
        WHERE t.workstream_id IN (
          SELECT workstream_id FROM workstreams WHERE repo_id = ?
        )
          AND t.lifecycle IN ('queued', 'running')
          AND a.status = 'completed'
        ORDER BY wr.created_at, wr.result_id
        """,
        (repo_id,),
    ).fetchall()
    finalized = 0
    for row in rows:
        result_row = row[:8]
        workstream_id = row[8]
        payload = _load_result_payload(conn, result_row)
        if not _result_audit_accepted(conn, payload["result_id"]):
            continue
        if not _result_actions_all_published(conn, payload["result_id"]):
            continue
        wakeup.consume_wakeups_for_results(
            conn,
            result_ids=[payload["result_id"]],
            run_id=run_id,
            now=run_now,
        )
        _finalize_accepted_result(
            conn,
            data_dir,
            db_path,
            repo,
            repo_id,
            payload,
            workstream_id,
            run_now,
            dry_run,
            dispatch_queue,
            dispatch_budget,
        )
        finalized += 1
    return finalized


def _publish_ready_actions_for_repo(db_path, dry_run, repo_id, repo_count):
    return publish.publish_ready_actions(db_path, dry_run=dry_run, repo_id=repo_id)


def _aggregate_repo_step_status(conn, run_id, step_key):
    rows = conn.execute(
        """
        SELECT status
        FROM run_repo_steps
        WHERE run_id = ?
          AND step_key = ?
        """,
        (run_id, step_key),
    ).fetchall()
    statuses = [row[0] for row in rows]
    if not statuses:
        return "pending"
    if "failed" in statuses:
        return "failed"
    if "running" in statuses:
        return "running"
    if "succeeded" in statuses:
        return "succeeded"
    if "skipped" in statuses:
        return "skipped"
    return "pending"


def _run_repo_pipeline(
    conn,
    *,
    data_dir,
    db_path,
    config_result,
    repo,
    repo_id,
    repo_count,
    run_id,
    run_now,
    dry_run,
    skip_external,
    skip_publish,
    fixture_path,
    discovery_runner,
    notification_hints,
    dispatch_queue,
    dispatch_budget,
    prompt_paths,
):
    summary = {
        "repo_id": repo_id,
        "full_name": repo["full_name"],
        "status": "completed",
        "failure_status": None,
        "lease_status": None,
        "safe_error": None,
        "supervised_count": 0,
        "failed_timeout_recovered_count": 0,
        "raw_event_count": 0,
        "decision_count": 0,
        "prompt_count": 0,
        "dispatch_queue_count": 0,
        "audited_result_count": 0,
        "publish_pending_count": 0,
        "published_count": 0,
        "publish_skipped_count": 0,
        "publish_failed_count": 0,
        "finalized_task_count": 0,
        "reconciled_source_count": 0,
        "current_state_closed_skip_count": 0,
    }
    lease_result = None
    publish_result = {
        "ok": True,
        "status": "skipped",
        "pending_count": 0,
        "published_count": 0,
        "skipped_count": 0,
        "failed_count": 0,
        "finalized_task_count": 0,
    }
    repo_prompt_start = len(prompt_paths)
    repo_dispatch_start = len(dispatch_queue)
    try:
        _mark_repo_run_step(conn, run_id, repo_id, "acquire_lease", "running", run_now)
        lease_result = _acquire_agent_lease(
            conn,
            repo_id,
            run_id,
            run_now,
            config_result.get("lease_ttl_minutes", 9),
        )
        summary["lease_status"] = lease_result["status"]
        if not lease_result["ok"]:
            summary["status"] = lease_result["status"]
            summary["failure_status"] = lease_result["status"]
            _mark_repo_run_step(
                conn,
                run_id,
                repo_id,
                "acquire_lease",
                "skipped",
                run_now,
                output=lease_result,
            )
            for step in REPO_STEP_ORDER[1:]:
                _mark_repo_run_step(
                    conn,
                    run_id,
                    repo_id,
                    step,
                    "skipped",
                    run_now,
                    output={"reason": lease_result["status"]},
                )
            return summary

        _mark_repo_run_step(
            conn,
            run_id,
            repo_id,
            "acquire_lease",
            "succeeded",
            run_now,
            output=lease_result,
        )

        _mark_repo_run_step(conn, run_id, repo_id, "supervise", "running", run_now)
        supervised_count = _supervise_running_attempts(
            conn,
            repo_id,
            config_result,
            run_now,
            data_dir=data_dir,
            db_path=db_path,
            repo=repo,
        )
        failed_timeout_recovered_count = _recover_failed_timeout_attempts(
            conn,
            repo_id,
            run_now,
            data_dir=data_dir,
            db_path=db_path,
            repo=repo,
        )
        supervised_count += failed_timeout_recovered_count
        summary["supervised_count"] = supervised_count
        summary["failed_timeout_recovered_count"] = failed_timeout_recovered_count
        _mark_repo_run_step(
            conn,
            run_id,
            repo_id,
            "supervise",
            "succeeded",
            run_now,
            output={
                "failed_timeout_recovered_count": failed_timeout_recovered_count,
                "supervised_count": supervised_count,
            },
        )

        _mark_repo_run_step(conn, run_id, repo_id, "audit_results", "running", run_now)
        try:
            audited_result_count = _audit_completed_results(
                conn,
                repo_id,
                data_dir,
                db_path,
                repo,
                run_id,
                run_now,
                dry_run,
                dispatch_queue,
                dispatch_budget,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            error = _safe_route_error(exc)
            summary["status"] = "failed"
            summary["failure_status"] = error["status"]
            summary["safe_error"] = error["safe_error"]
            _mark_repo_run_step(
                conn,
                run_id,
                repo_id,
                "audit_results",
                "failed",
                _now(),
                error=error,
            )
            for step in ["publish_actions", "reconcile", "discover", "authorize", "route"]:
                _mark_repo_run_step(
                    conn,
                    run_id,
                    repo_id,
                    step,
                    "skipped",
                    run_now,
                    output={"reason": error["status"]},
                )
            return summary
        summary["audited_result_count"] = audited_result_count
        _mark_repo_run_step(
            conn,
            run_id,
            repo_id,
            "audit_results",
            "succeeded",
            run_now,
            output={"audited_result_count": audited_result_count},
        )

        if skip_publish:
            publish_result = {
                "ok": True,
                "status": "skipped",
                "reason": "skip_publish",
                "pending_count": 0,
                "published_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
                "finalized_task_count": 0,
            }
            _mark_repo_run_step(
                conn,
                run_id,
                repo_id,
                "publish_actions",
                "skipped",
                run_now,
                output=publish_result,
            )
        else:
            _mark_repo_run_step(conn, run_id, repo_id, "publish_actions", "running", run_now)
            conn.commit()
            try:
                publish_result = _publish_ready_actions_for_repo(
                    db_path,
                    dry_run,
                    repo_id,
                    repo_count,
                )
            except Exception as exc:
                publish_result = _safe_publish_error(exc)
            publish_failed = not publish_result["ok"]
            try:
                finalized_after_publish_count = _finalize_published_results(
                    conn,
                    repo_id,
                    data_dir,
                    db_path,
                    repo,
                    run_id,
                    run_now,
                    dry_run,
                    dispatch_queue,
                    dispatch_budget,
                )
            except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                error = _safe_route_error(exc)
                summary["status"] = "failed"
                summary["failure_status"] = error["status"]
                summary["safe_error"] = error["safe_error"]
                _mark_repo_run_step(
                    conn,
                    run_id,
                    repo_id,
                    "publish_actions",
                    "failed",
                    _now(),
                    error=error,
                )
                for step in ["reconcile", "discover", "authorize", "route"]:
                    _mark_repo_run_step(
                        conn,
                        run_id,
                        repo_id,
                        step,
                        "skipped",
                        run_now,
                        output={"reason": error["status"]},
                    )
                return summary
            publish_result = {
                **publish_result,
                "finalized_task_count": finalized_after_publish_count,
            }
            if publish_failed:
                summary["status"] = "failed"
                summary["failure_status"] = publish_result.get("status", "publish_failed")
                summary["safe_error"] = publish_result.get(
                    "safe_error",
                    "GitHub action publication failed",
                )
                _mark_repo_run_step(
                    conn,
                    run_id,
                    repo_id,
                    "publish_actions",
                    "failed",
                    run_now,
                    output=publish_result,
                    error=publish_result,
                )
            else:
                _mark_repo_run_step(
                    conn,
                    run_id,
                    repo_id,
                    "publish_actions",
                    "succeeded",
                    run_now,
                    output=publish_result,
                )
        summary["publish_pending_count"] = publish_result.get("pending_count", 0)
        summary["published_count"] = publish_result.get("published_count", 0)
        summary["publish_skipped_count"] = publish_result.get("skipped_count", 0)
        summary["publish_failed_count"] = publish_result.get("failed_count", 0)
        summary["finalized_task_count"] = publish_result.get("finalized_task_count", 0)

        if fixture_path or skip_external:
            _mark_repo_run_step(
                conn,
                run_id,
                repo_id,
                "reconcile",
                "skipped",
                run_now,
                output={"reason": "skip_external" if skip_external else "fixture_path"},
            )
        else:
            _mark_repo_run_step(conn, run_id, repo_id, "reconcile", "running", run_now)
            reconciled_source_count = _reconcile_remote_source_states(
                conn,
                repo_id,
                repo,
                discovery_runner or subprocess.run,
                run_now,
            )
            summary["reconciled_source_count"] = reconciled_source_count
            _mark_repo_run_step(
                conn,
                run_id,
                repo_id,
                "reconcile",
                "succeeded",
                run_now,
                output={"reconciled_source_count": reconciled_source_count},
            )

        known_workstreams = _load_known_workstreams(conn, repo_id)
        review_participants = _load_review_participants(conn, repo_id)
        active_workstreams = _load_active_workstreams(conn, repo_id)
        _mark_repo_run_step(conn, run_id, repo_id, "discover", "running", run_now)
        if fixture_path:
            raw_events = discover.load_fixture(fixture_path)
        elif skip_external:
            raw_events = []
        else:
            try:
                discovery_kwargs = {"known_workstreams": known_workstreams}
                if notification_hints is not None:
                    discovery_kwargs["notification_hints"] = notification_hints
                    discovery_kwargs["include_notifications"] = False
                if discovery_runner is not None:
                    discovery_kwargs["runner"] = discovery_runner
                raw_events = discover.collect_live_events(repo, **discovery_kwargs)
            except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as exc:
                error = _safe_discovery_error(exc)
                summary["status"] = "failed"
                summary["failure_status"] = error["status"]
                summary["safe_error"] = error["safe_error"]
                _mark_repo_run_step(
                    conn,
                    run_id,
                    repo_id,
                    "discover",
                    "failed",
                    _now(),
                    error=error,
                )
                for step in ["authorize", "route"]:
                    _mark_repo_run_step(
                        conn,
                        run_id,
                        repo_id,
                        step,
                        "skipped",
                        run_now,
                        output={"reason": error["status"]},
                    )
                return summary
        summary["raw_event_count"] = len(raw_events)
        _mark_repo_run_step(
            conn,
            run_id,
            repo_id,
            "discover",
            "succeeded",
            run_now,
            output={"raw_event_count": len(raw_events)},
        )

        _mark_repo_run_step(conn, run_id, repo_id, "authorize", "running", run_now)
        try:
            normalized = discover.normalize_events(raw_events, repo)
            normalized = _attach_review_participants(normalized, review_participants)
        except (KeyError, TypeError, ValueError) as exc:
            error = _safe_discovery_error(exc)
            summary["status"] = "failed"
            summary["failure_status"] = error["status"]
            summary["safe_error"] = error["safe_error"]
            _mark_repo_run_step(
                conn,
                run_id,
                repo_id,
                "discover",
                "failed",
                _now(),
                error=error,
            )
            for step in ["authorize", "route"]:
                _mark_repo_run_step(
                    conn,
                    run_id,
                    repo_id,
                    step,
                    "skipped",
                    run_now,
                    output={"reason": error["status"]},
                )
            return summary
        decisions = authorize.authorize_events(
            normalized,
            repo,
            known_workstreams=known_workstreams,
        )
        decisions = sorted(decisions, key=_event_priority)
        summary["decision_count"] = len(decisions)
        _mark_repo_run_step(
            conn,
            run_id,
            repo_id,
            "authorize",
            "succeeded",
            run_now,
            output={"decision_count": len(decisions)},
        )

        _mark_repo_run_step(conn, run_id, repo_id, "route", "running", run_now)
        if not dry_run and dispatch_budget["remaining"] > 0:
            queued_prepared_count = _queue_prepared_attempts(
                conn,
                repo_id,
                repo,
                dispatch_queue,
                dispatch_budget["remaining"],
            )
            dispatch_budget["remaining"] = max(
                0,
                dispatch_budget["remaining"] - queued_prepared_count,
            )
        if dispatch_budget["remaining"] > 0:
            _prepare_detected_local_tasks(
                conn,
                data_dir,
                db_path,
                repo,
                repo_id,
                run_id,
                run_now,
                dry_run,
                dispatch_queue,
                dispatch_budget,
                prompt_paths,
            )
        route_error = None

        for event in decisions:
            source_id, event_id = _insert_source_and_event(conn, repo_id, event, run_now)
            if _event_has_terminal_trigger(conn, event["event_fingerprint"]):
                continue
            if _is_driving_context_event(event):
                waiting_task = _waiting_task_state(conn, event["workstream_id"])
                if waiting_task:
                    if not _is_trusted_actor(event, repo):
                        _append_event_to_active_task(conn, event, "context", run_now)
                        continue
                    work_items.record_github_response(
                        conn,
                        event["workstream_id"],
                        actor_identity=event.get("actor_login") or "unknown",
                        body=event.get("body") or "",
                        event_fingerprint=event["event_fingerprint"],
                        metadata={"source_key": event.get("source_key")},
                        now=run_now,
                    )
                    waiting_context_events = _pending_events_for_task(
                        conn,
                        waiting_task["task_id"],
                        [],
                    )
                    stream = {
                        "workstream_id": event["workstream_id"],
                        "source_relationship": workstream.source_relationship(event),
                    }
                    route_result = _effective_route(
                        config_result,
                        repo,
                        event,
                    )
                    try:
                        worktree_result = _prepare_worktree(repo, event, route_result, dry_run)
                    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                        route_error = _safe_route_error(exc)
                        break
                    conn.execute(
                        "UPDATE tasks SET lifecycle = 'completed', updated_at = ? WHERE task_id = ?",
                        (run_now, waiting_task["task_id"]),
                    )
                    task_info = _create_task_attempt_and_prompt(
                        conn,
                        data_dir,
                        db_path,
                        repo,
                        repo_id,
                        event,
                        route_result,
                        stream,
                        run_now,
                        worktree_result=worktree_result,
                        parent_task_id=waiting_task["task_id"],
                        related_events=[event] + waiting_context_events,
                    )
                    _queue_dispatch(
                        dispatch_queue,
                        task_info,
                        worktree_result,
                        dispatch_budget,
                        repo_id=repo_id,
                        repo=repo,
                    )
                    prompt_paths.append(str(task_info["prompt_path"]))
                    active_workstreams.add(task_info["workstream_id"])
                    known_workstreams.add(task_info["workstream_id"])
                    continue
                if event["workstream_id"] in active_workstreams:
                    appended = _append_event_to_active_task(conn, event, "context", run_now)
                    if appended:
                        try:
                            refreshed_prompt = _refresh_prepared_task(
                                conn,
                                data_dir,
                                db_path,
                                repo,
                                event,
                                run_now,
                                dry_run,
                            )
                        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                            route_error = _safe_route_error(exc)
                            break
                        if refreshed_prompt:
                            prompt_paths.append(str(refreshed_prompt))
                    continue
                if not (
                    _is_trusted_actor(event, repo)
                    or _is_dd_pr_followup_event(event)
                    or event.get("authorization_status") == "accepted_review_participant"
                ):
                    continue
                if _record_current_state_closed_source_if_needed(
                    conn,
                    repo_id,
                    source_id,
                    event_id,
                    event,
                    run_now,
                ):
                    summary["current_state_closed_skip_count"] += 1
                    continue
                retry_parent_id = _latest_task_id(conn, event["workstream_id"])
                retry_context = _failed_parent_context_events(conn, retry_parent_id)
                task_event = retry_context[0] if retry_context else event
                retry_related = (
                    _dedupe_events_by_fingerprint(retry_context + [event])
                    if retry_context
                    else None
                )
                stream = workstream.plan_event(task_event, active_workstreams=active_workstreams)
                route_result = _effective_route(
                    config_result,
                    repo,
                    task_event,
                )
                try:
                    worktree_result = _prepare_worktree(repo, task_event, route_result, dry_run)
                except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                    route_error = _safe_route_error(exc)
                    break
                task_info = _create_task_attempt_and_prompt(
                    conn,
                    data_dir,
                    db_path,
                    repo,
                    repo_id,
                    task_event,
                    route_result,
                    stream,
                    run_now,
                    worktree_result=worktree_result,
                    parent_task_id=retry_parent_id,
                    related_events=retry_related,
                )
                _queue_dispatch(
                    dispatch_queue,
                    task_info,
                    worktree_result,
                    dispatch_budget,
                    repo_id=repo_id,
                    repo=repo,
                )
                prompt_paths.append(str(task_info["prompt_path"]))
                active_workstreams.add(task_info["workstream_id"])
                known_workstreams.add(task_info["workstream_id"])
                continue
            if event["authorization_status"] != "authorized_trigger":
                continue
            stream = workstream.plan_event(event, active_workstreams=active_workstreams)
            if stream["action"] == "append_pending_event":
                _append_event_to_active_task(conn, event, "pending", run_now)
                continue
            if _record_current_state_closed_source_if_needed(
                conn,
                repo_id,
                source_id,
                event_id,
                event,
                run_now,
            ):
                summary["current_state_closed_skip_count"] += 1
                continue
            route_result = _effective_route(
                config_result,
                repo,
                event,
            )
            try:
                worktree_result = _prepare_worktree(repo, event, route_result, dry_run)
            except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                route_error = _safe_route_error(exc)
                break
            task_info = _create_task_attempt_and_prompt(
                conn,
                data_dir,
                db_path,
                repo,
                repo_id,
                event,
                route_result,
                stream,
                run_now,
                worktree_result=worktree_result,
            )
            _queue_dispatch(
                dispatch_queue,
                task_info,
                worktree_result,
                dispatch_budget,
                repo_id=repo_id,
                repo=repo,
            )
            prompt_paths.append(str(task_info["prompt_path"]))
            active_workstreams.add(task_info["workstream_id"])

        if route_error:
            summary["status"] = "failed"
            summary["failure_status"] = route_error["status"]
            summary["safe_error"] = route_error["safe_error"]
            _mark_repo_run_step(
                conn,
                run_id,
                repo_id,
                "route",
                "failed",
                _now(),
                output={
                    "prompt_count": len(prompt_paths) - repo_prompt_start,
                    "dispatch_queue_count": len(dispatch_queue) - repo_dispatch_start,
                    "reconciled_source_count": summary["reconciled_source_count"],
                    "current_state_closed_skip_count": summary["current_state_closed_skip_count"],
                },
                error=route_error,
            )
            return summary

        _mark_repo_run_step(
            conn,
            run_id,
            repo_id,
            "route",
            "succeeded",
            run_now,
            output={
                "prompt_count": len(prompt_paths) - repo_prompt_start,
                "dispatch_queue_count": len(dispatch_queue) - repo_dispatch_start,
                "reconciled_source_count": summary["reconciled_source_count"],
                "current_state_closed_skip_count": summary["current_state_closed_skip_count"],
            },
        )
        return summary
    finally:
        if lease_result and lease_result.get("ok"):
            _release_agent_lease(conn, lease_result["lease_id"], _now())
        summary["prompt_count"] = len(prompt_paths) - repo_prompt_start
        summary["dispatch_queue_count"] = len(dispatch_queue) - repo_dispatch_start


def run_once(
    config_path,
    workflow_path=None,
    fixture_path=None,
    dry_run=True,
    skip_external=False,
    skip_publish=False,
    discovery_runner=None,
    max_dispatches=None,
):
    config_result = validate_config.validate_config(config_path, skip_external=skip_external)
    if not config_result["ok"]:
        return config_result
    if workflow_path and not Path(workflow_path).exists():
        return {
            "ok": False,
            "status": "failed_config",
            "safe_error": f"workflow not found: {workflow_path}",
        }
    data_dir = Path(config_result["data_dir"])
    db_path = Path(config_result["db_path"])
    storage.init_database(db_path)
    run_id = _id("run")
    run_now = _now()
    prompt_paths = []
    dispatch_queue = []

    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            """
            INSERT INTO agent_runs(run_id, status, started_at, config_path, dry_run)
            VALUES (?, 'running', ?, ?, ?)
            """,
            (run_id, run_now, str(config_path), 1 if dry_run else 0),
        )
        for step in [
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
        ]:
            _insert_run_step(conn, run_id, step)
        _mark_run_step(
            conn,
            run_id,
            "validate_config",
            "succeeded",
            run_now,
            output={"repo_count": len(config_result["repos"])},
        )

        repos = []
        repo_ids = []
        for repo_config in config_result["repos"]:
            repo = _with_python_bin(repo_config, config_result)
            repo_id = _upsert_repo(conn, repo)
            repos.append((repo_id, repo))
            repo_ids.append(repo_id)
            _init_repo_steps(conn, run_id, repo_id)

        notification_buckets = {}
        if not fixture_path and not skip_external:
            _mark_run_step(conn, run_id, "discover_notifications", "running", run_now)
            try:
                notification_buckets = discover.collect_account_notifications(
                    [repo for _repo_id, repo in repos],
                    runner=discovery_runner or subprocess.run,
                )
                _mark_run_step(
                    conn,
                    run_id,
                    "discover_notifications",
                    "succeeded",
                    run_now,
                    output={"notification_repo_count": len(notification_buckets)},
                )
            except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as exc:
                error = _safe_discovery_error(exc)
                _mark_run_step(
                    conn,
                    run_id,
                    "discover_notifications",
                    "failed",
                    _now(),
                    error=error,
                )
                notification_buckets = None
        else:
            _mark_run_step(
                conn,
                run_id,
                "discover_notifications",
                "skipped",
                run_now,
                output={"reason": "skip_external" if skip_external else "fixture_path"},
            )

        max_concurrency = config_result.get("max_concurrency", 1)
        capacity_remaining = max(
            0,
            max_concurrency - _running_attempt_count_for_repos(conn, repo_ids),
        )
        if max_dispatches is not None:
            capacity_remaining = min(capacity_remaining, max(0, int(max_dispatches)))
        dispatch_budget = {"remaining": capacity_remaining}

        _mark_run_step(conn, run_id, "repo_pipelines", "running", run_now)
        repo_summaries = []
        for repo_id, repo in repos:
            notification_hints = None
            if notification_buckets is not None:
                notification_hints = notification_buckets.get(repo["full_name"], [])
            summary = _run_repo_pipeline(
                conn,
                data_dir=data_dir,
                db_path=db_path,
                config_result=config_result,
                repo=repo,
                repo_id=repo_id,
                repo_count=len(repos),
                run_id=run_id,
                run_now=run_now,
                dry_run=dry_run,
                skip_external=skip_external,
                skip_publish=skip_publish,
                fixture_path=fixture_path,
                discovery_runner=discovery_runner,
                notification_hints=notification_hints,
                dispatch_queue=dispatch_queue,
                dispatch_budget=dispatch_budget,
                prompt_paths=prompt_paths,
            )
            repo_summaries.append(summary)

        aggregate_counts = {
            "supervised_count": sum(item["supervised_count"] for item in repo_summaries),
            "failed_timeout_recovered_count": sum(
                item["failed_timeout_recovered_count"] for item in repo_summaries
            ),
            "audited_result_count": sum(item["audited_result_count"] for item in repo_summaries),
            "reconciled_source_count": sum(
                item["reconciled_source_count"] for item in repo_summaries
            ),
            "raw_event_count": sum(item["raw_event_count"] for item in repo_summaries),
            "decision_count": sum(item["decision_count"] for item in repo_summaries),
            "current_state_closed_skip_count": sum(
                item["current_state_closed_skip_count"] for item in repo_summaries
            ),
            "publish_pending_count": sum(
                item["publish_pending_count"] for item in repo_summaries
            ),
            "published_count": sum(item["published_count"] for item in repo_summaries),
            "publish_skipped_count": sum(
                item["publish_skipped_count"] for item in repo_summaries
            ),
            "publish_failed_count": sum(item["publish_failed_count"] for item in repo_summaries),
            "finalized_task_count": sum(item["finalized_task_count"] for item in repo_summaries),
        }
        publish_result = {
            "ok": aggregate_counts["publish_failed_count"] == 0,
            "status": (
                "skipped"
                if skip_publish
                else (
                    "publish_failed"
                    if aggregate_counts["publish_failed_count"]
                    else "dry_run"
                    if dry_run
                    else "published"
                )
            ),
            "pending_count": aggregate_counts["publish_pending_count"],
            "published_count": aggregate_counts["published_count"],
            "skipped_count": aggregate_counts["publish_skipped_count"],
            "failed_count": aggregate_counts["publish_failed_count"],
            "finalized_task_count": aggregate_counts["finalized_task_count"],
        }
        if skip_publish:
            publish_result["reason"] = "skip_publish"

        step_outputs = {
            "acquire_lease": {
                "repo_count": len(repo_summaries),
                "acquired_count": sum(
                    1 for item in repo_summaries if item["lease_status"] == "acquired"
                ),
                "skipped_active_lease_count": sum(
                    1
                    for item in repo_summaries
                    if item["lease_status"] == "skipped_active_lease"
                ),
            },
            "supervise": {
                "failed_timeout_recovered_count": aggregate_counts[
                    "failed_timeout_recovered_count"
                ],
                "supervised_count": aggregate_counts["supervised_count"],
            },
            "audit_results": {
                "audited_result_count": aggregate_counts["audited_result_count"],
            },
            "publish_actions": publish_result,
            "reconcile": {
                "reconciled_source_count": aggregate_counts["reconciled_source_count"],
            },
            "discover": {
                "raw_event_count": aggregate_counts["raw_event_count"],
            },
            "authorize": {
                "decision_count": aggregate_counts["decision_count"],
            },
            "route": {
                "prompt_count": len(prompt_paths),
                "dispatch_queue_count": len(dispatch_queue),
                "reconciled_source_count": aggregate_counts["reconciled_source_count"],
                "current_state_closed_skip_count": aggregate_counts[
                    "current_state_closed_skip_count"
                ],
            },
        }

        for step in REPO_STEP_ORDER:
            failed_summary = next(
                (
                    item
                    for item in repo_summaries
                    if item["status"] == "failed" and item.get("safe_error")
                ),
                None,
            )
            error = None
            if _aggregate_repo_step_status(conn, run_id, step) == "failed" and failed_summary:
                error = {
                    "status": failed_summary.get("failure_status", "failed"),
                    "safe_error": failed_summary["safe_error"],
                    "repo_id": failed_summary["repo_id"],
                    "full_name": failed_summary["full_name"],
                }
            _mark_run_step(
                conn,
                run_id,
                step,
                _aggregate_repo_step_status(conn, run_id, step),
                run_now,
                output=step_outputs.get(step),
                error=error,
            )

        failed_repos = [item for item in repo_summaries if item["status"] == "failed"]
        succeeded_repos = [
            item
            for item in repo_summaries
            if item["status"] in {"completed", "skipped_active_lease"}
        ]
        if repo_summaries and all(
            item["status"] == "skipped_active_lease" for item in repo_summaries
        ):
            overall_status = "skipped_active_lease"
        elif failed_repos and succeeded_repos:
            overall_status = "partial_failure"
        elif failed_repos:
            overall_status = "failed"
        else:
            overall_status = "completed"

        repo_pipeline_status = (
            "skipped"
            if overall_status == "skipped_active_lease"
            else ("failed" if failed_repos else "succeeded")
        )
        _mark_run_step(
            conn,
            run_id,
            "repo_pipelines",
            repo_pipeline_status,
            run_now,
            output={"repo_summaries": repo_summaries, "overall_status": overall_status},
        )

        if overall_status == "skipped_active_lease":
            finished_at = _now()
            for step in ["dispatch", "summarize"]:
                _mark_run_step(
                    conn,
                    run_id,
                    step,
                    "skipped",
                    finished_at,
                    output={"reason": "skipped_active_lease"},
                )
            summary = {
                "overall_status": overall_status,
                "repo_summaries": repo_summaries,
                "prompt_count": len(prompt_paths),
                "dispatch_count": 0,
            }
            conn.execute(
                """
                UPDATE agent_runs
                SET status = 'skipped', finished_at = ?, summary_json = ?
                WHERE run_id = ?
                """,
                (
                    finished_at,
                    json.dumps(summary, sort_keys=True),
                    run_id,
                ),
            )
            return {
                "ok": True,
                "status": "skipped_active_lease",
                "run_id": run_id,
                "db_path": str(db_path),
                "prompt_paths": [],
                "repo_summaries": repo_summaries,
            }

    dispatch_results = []
    dispatch_error = None
    try:
        dispatch_results = _dispatch_queued(
            db_path,
            repo_ids,
            dispatch_queue,
            dry_run,
            max_concurrency,
            config_result.get("workers"),
            config_result.get("default_worker"),
            config_result.get("route_worker_models"),
            config_result,
        )
    except Exception as exc:
        dispatch_error = _safe_dispatch_error(exc)
    finally:
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            finished_at = _now()
            dispatch_failure = dispatch_error or next(
                (
                    result
                    for result in dispatch_results
                    if not result.get("ok", True) or result.get("status") == "failed"
                ),
                None,
            )
            if dispatch_failure:
                _mark_run_step(
                    conn,
                    run_id,
                    "dispatch",
                    "failed",
                    finished_at,
                    output={"dispatch_count": len(dispatch_results)},
                    error=dispatch_failure,
                )
                _mark_run_step(
                    conn,
                    run_id,
                    "summarize",
                    "skipped",
                    finished_at,
                    output={"reason": dispatch_failure.get("status", "failed_dispatch")},
                )
                conn.execute(
                    """
                    UPDATE agent_runs
                    SET status = 'failed', finished_at = ?, summary_json = ?, error_json = ?
                    WHERE run_id = ?
                    """,
                    (
                        finished_at,
                        json.dumps(
                            {
                                "overall_status": "failed",
                                "repo_summaries": repo_summaries,
                                "prompt_count": len(prompt_paths),
                                "dispatch_count": len(dispatch_results),
                                "audited_result_count": aggregate_counts["audited_result_count"],
                                "publish_result": publish_result,
                                "reconciled_source_count": aggregate_counts[
                                    "reconciled_source_count"
                                ],
                                "current_state_closed_skip_count": aggregate_counts[
                                    "current_state_closed_skip_count"
                                ],
                                "supervised_count": aggregate_counts["supervised_count"],
                            },
                            sort_keys=True,
                        ),
                        json.dumps(dispatch_failure, ensure_ascii=False, sort_keys=True),
                        run_id,
                    ),
                )
            else:
                summary = {
                    "overall_status": overall_status,
                    "repo_summaries": repo_summaries,
                    "prompt_count": len(prompt_paths),
                    "dispatch_count": len(dispatch_results),
                    "audited_result_count": aggregate_counts["audited_result_count"],
                    "publish_result": publish_result,
                    "reconciled_source_count": aggregate_counts["reconciled_source_count"],
                    "current_state_closed_skip_count": aggregate_counts[
                        "current_state_closed_skip_count"
                    ],
                    "supervised_count": aggregate_counts["supervised_count"],
                }
                _mark_run_step(
                    conn,
                    run_id,
                    "dispatch",
                    "succeeded",
                    finished_at,
                    output={"dispatch_count": len(dispatch_results)},
                )
                _mark_run_step(
                    conn,
                    run_id,
                    "summarize",
                    "succeeded",
                    finished_at,
                    output=summary,
                )
                first_repo_error = next(
                    (
                        {
                            "status": item.get("failure_status", "failed"),
                            "safe_error": item.get("safe_error"),
                            "repo_id": item["repo_id"],
                            "full_name": item["full_name"],
                        }
                        for item in repo_summaries
                        if item["status"] == "failed"
                    ),
                    None,
                )
                persisted_status = (
                    "failed" if overall_status in {"failed", "partial_failure"} else "completed"
                )
                if persisted_status == "completed":
                    conn.execute(
                        """
                        UPDATE agent_runs
                        SET status = 'completed', finished_at = ?, summary_json = ?
                        WHERE run_id = ?
                        """,
                        (
                            finished_at,
                            json.dumps(summary, sort_keys=True),
                            run_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE agent_runs
                        SET status = 'failed', finished_at = ?, summary_json = ?, error_json = ?
                        WHERE run_id = ?
                        """,
                        (
                            finished_at,
                            json.dumps(summary, sort_keys=True),
                            json.dumps(
                                first_repo_error or publish_result,
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            run_id,
                        ),
                    )
    dispatch_failure = dispatch_error or next(
        (
            result
            for result in dispatch_results
            if not result.get("ok", True) or result.get("status") == "failed"
        ),
        None,
    )
    if dispatch_failure:
        return {
            "ok": False,
            "status": dispatch_failure.get("status", "failed_dispatch"),
            "run_id": run_id,
            "db_path": str(db_path),
            "prompt_paths": prompt_paths,
            "dispatch_count": len(dispatch_results),
            "publish_result": publish_result,
            "safe_error": dispatch_failure.get("safe_error", "worker dispatch failed"),
        }
    if overall_status == "partial_failure":
        return {
            "ok": False,
            "status": "partial_failure",
            "run_id": run_id,
            "db_path": str(db_path),
            "prompt_paths": prompt_paths,
            "dispatch_count": len(dispatch_results),
            "publish_result": publish_result,
            "repo_summaries": repo_summaries,
        }
    if overall_status == "failed":
        failure_summary = next(
            (item for item in repo_summaries if item["status"] == "failed"),
            None,
        )
        failure_status = (
            failure_summary.get("failure_status")
            if failure_summary and len(repo_summaries) == 1
            else "failed"
        )
        return {
            "ok": False,
            "status": failure_status,
            "run_id": run_id,
            "db_path": str(db_path),
            "prompt_paths": prompt_paths,
            "dispatch_count": len(dispatch_results),
            "publish_result": publish_result,
            "repo_summaries": repo_summaries,
            "safe_error": failure_summary.get("safe_error") if failure_summary else None,
        }
    return {
        "ok": True,
        "status": "completed",
        "run_id": run_id,
        "db_path": str(db_path),
        "prompt_paths": prompt_paths,
        "dispatch_count": len(dispatch_results),
        "publish_result": publish_result,
        "repo_summaries": repo_summaries,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--workflow")
    parser.add_argument("--fixture")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-external", action="store_true")
    parser.add_argument("--skip-publish", action="store_true")
    parser.add_argument("--max-dispatches", type=int)
    args = parser.parse_args(argv)
    result = run_once(
        args.config,
        workflow_path=args.workflow,
        fixture_path=args.fixture,
        dry_run=args.dry_run,
        skip_external=args.skip_external,
        skip_publish=args.skip_publish,
        max_dispatches=args.max_dispatches,
    )
    return emit(result, 0 if result.get("ok") else 3)


if __name__ == "__main__":
    raise SystemExit(main())
