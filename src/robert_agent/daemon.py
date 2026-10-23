#!/usr/bin/env python3
import argparse
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

from robert_agent.common import emit
from robert_agent import daemon_state, dbos_runtime, redaction, retention, storage, wakeup_notifier
from robert_agent import loop_engine
from robert_agent import validate_config
from robert_agent import workflow_signals


SCRIPT_ROOT = Path(__file__).resolve().parent
AGENTS_ROOT = SCRIPT_ROOT.parents[1]
_STOP_REQUESTED = False


@dataclass
class DaemonContext:
    config_path: str
    workflow_path: str | None
    db_path: Path
    repo_id: str
    max_concurrency: int
    daemon_config: dict
    daemon_run_id: str
    lease_id: str
    repo_ids: list[str] | None = None
    dry_run: bool = False
    next_live_poll_at: str | None = None
    startup_live_poll_pending: bool = False
    rate_limit_decision_cache: dict | None = None
    rate_limit_cache_expires_at: str | None = None
    next_maintenance_at: float = 0.0
    workflow_engine: dict | None = None


def _script_path(name):
    return str(SCRIPT_ROOT / name)


def _workflow_args(workflow_path):
    return ["--workflow", str(workflow_path)] if workflow_path else []


def build_run_once_command(config_path, workflow_path=None, *, dry_run=False):
    command = [
        sys.executable,
        "-B",
        _script_path("run_once.py"),
        "--config",
        str(config_path),
        *_workflow_args(workflow_path),
    ]
    if dry_run:
        command.append("--dry-run")
    return command


def build_loop_command(
    config_path,
    workflow_path=None,
    *,
    dry_run=False,
    max_seconds=180,
    skip_publish=False,
):
    command = [
        sys.executable,
        "-B",
        _script_path("loop_engine.py"),
        "--config",
        str(config_path),
        "--skip-external",
        "--max-seconds",
        str(max_seconds),
        *_workflow_args(workflow_path),
    ]
    if dry_run:
        command.append("--dry-run")
    if skip_publish:
        command.append("--skip-publish")
    return command


def _short_text(value, max_chars=2000):
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}...[truncated {len(text) - max_chars} chars]"


def run_child_json(command, *, timeout_seconds, runner=subprocess.run):
    try:
        completed = runner(
            command,
            cwd=str(AGENTS_ROOT),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "status": "child_timeout",
            "safe_error": f"child timed out after {timeout_seconds}s",
            "child": {
                "command": command,
                "timeout_seconds": timeout_seconds,
                "stdout": _short_text(getattr(exc, "stdout", "")),
                "stderr": _short_text(getattr(exc, "stderr", "")),
            },
        }
    stdout = completed.stdout.strip()
    child = {
        "command": command,
        "returncode": completed.returncode,
        "stdout": _short_text(completed.stdout),
        "stderr": _short_text(completed.stderr),
    }
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "status": "child_failed",
            "safe_error": _short_text(completed.stderr or completed.stdout or "child returned non-JSON output"),
            "child": child,
        }
    payload = payload if isinstance(payload, dict) else {"payload": payload}
    if completed.returncode != 0:
        return {
            **payload,
            "ok": False,
            "status": payload.get("status") or "child_failed",
            "safe_error": payload.get("safe_error") or _short_text(completed.stderr or "child failed"),
            "child": child,
        }
    return {**payload, "child": child}


def parse_rate_limit(payload):
    resources = payload.get("resources") if isinstance(payload, dict) else {}
    resources = resources if isinstance(resources, dict) else {}
    core = resources.get("core") if isinstance(resources.get("core"), dict) else {}
    search = resources.get("search") if isinstance(resources.get("search"), dict) else {}
    return {
        "core": {
            "remaining": int(core.get("remaining", 0)),
            "reset": core.get("reset"),
        },
        "search": {
            "remaining": int(search.get("remaining", 0)),
            "reset": search.get("reset"),
        },
    }


def should_skip_live_poll_for_rate_limit(rate_limit, daemon_config):
    core = rate_limit.get("core") or {}
    search = rate_limit.get("search") or {}
    if search.get("remaining", 0) < daemon_config["min_search_remaining"]:
        return {
            "skip": True,
            "reason": "search_rate_limit_floor",
            "rate_limit": rate_limit,
        }
    if core.get("remaining", 0) < daemon_config["min_core_remaining"]:
        return {
            "skip": True,
            "reason": "core_rate_limit_floor",
            "rate_limit": rate_limit,
        }
    return {"skip": False, "reason": "rate_limit_ok", "rate_limit": rate_limit}


def fetch_rate_limit(runner=subprocess.run):
    result = run_child_json(
        ["gh", "api", "rate_limit"],
        timeout_seconds=30,
        runner=runner,
    )
    if not result.get("ok", True):
        return result
    return {
        "ok": True,
        "status": "rate_limit_loaded",
        "rate_limit": parse_rate_limit(result),
    }


def _request_stop(_signum, _frame):
    global _STOP_REQUESTED
    _STOP_REQUESTED = True


def _daemon_run_id():
    return f"daemon-run-{uuid4().hex[:12]}"


def _daemon_resource_key(config_result):
    db_path = Path(config_result["db_path"]).expanduser().resolve()
    return f"database:{db_path}"


def _parse_time(value):
    if isinstance(value, datetime):
        return value
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _iso_after(now, seconds):
    return (_parse_time(now) + timedelta(seconds=seconds)).isoformat()


def _lease_ttl_seconds(daemon_config):
    heartbeat_margin = max(daemon_config["local_poll_seconds"] * 3, 30)
    return (
        daemon_config["live_run_timeout_seconds"]
        + daemon_config["local_drain_timeout_seconds"]
        + heartbeat_margin
    )


def _live_poll_interval_seconds(context, *, capacity_full):
    if capacity_full:
        return context.daemon_config["github_poll_when_full_seconds"]
    return context.daemon_config["github_poll_seconds"]


def _schedule_next_live_poll(context, *, now, capacity_full):
    interval_seconds = _live_poll_interval_seconds(context, capacity_full=capacity_full)
    context.next_live_poll_at = _iso_after(now, interval_seconds)
    return interval_seconds


def _idle_sleep_seconds(context, *, now=None):
    now = now or daemon_state.utc_now()
    local_poll_seconds = context.daemon_config["local_poll_seconds"]
    if context.next_live_poll_at is None:
        return local_poll_seconds
    remaining_seconds = max(
        0,
        int((_parse_time(context.next_live_poll_at) - _parse_time(now)).total_seconds()),
    )
    return min(local_poll_seconds, remaining_seconds)


def _sleep_seconds_after_decision(context, result, *, now=None):
    if result.get("decision") == "idle":
        return _idle_sleep_seconds(context, now=now)
    if result.get("decision") == "local_drain":
        if not result.get("ok", False):
            return context.daemon_config["local_poll_seconds"]
        if (result.get("result") or {}).get("stop_reason") == "no_progress":
            return context.daemon_config["local_poll_seconds"]
    return 0


def _wait_for_next_decision(
    context,
    result,
    *,
    listener=None,
    sleeper=time.sleep,
    now=None,
):
    sleep_seconds = _sleep_seconds_after_decision(context, result, now=now)
    if sleep_seconds <= 0:
        return False
    if listener is not None:
        return listener.wait(sleep_seconds)
    sleeper(sleep_seconds)
    return False


def _lookup_repo_id(conn, full_name, fallback):
    row = conn.execute(
        "SELECT repo_id FROM repos WHERE full_name = ?",
        (full_name,),
    ).fetchone()
    return row[0] if row else fallback


def _load_context(config_path, workflow_path=None, *, dry_run=False, now=None):
    config_result = validate_config.validate_config(config_path, skip_external=True)
    if not config_result.get("ok"):
        return None, config_result
    db_path = Path(config_result["db_path"])
    storage.init_database(db_path)
    if not config_result["daemon"]["enabled"]:
        return None, {
            "ok": True,
            "status": "disabled",
            "decision": "disabled",
            "db_path": str(db_path),
        }
    daemon_run_id = _daemon_run_id()
    resource_key = _daemon_resource_key(config_result)
    daemon_config = config_result["daemon"]
    ttl_seconds = _lease_ttl_seconds(daemon_config)
    with closing(daemon_state.connect(db_path)) as conn, conn:
        lease = daemon_state.acquire_daemon_lease(
            conn,
            resource_key=resource_key,
            owner_id=daemon_run_id,
            ttl_seconds=ttl_seconds,
            now=now,
        )
        if not lease["ok"]:
            return None, {"ok": True, "status": lease["status"], "lease": lease}
        repo_ids = [
            _lookup_repo_id(
                conn,
                repo["full_name"],
                daemon_state.repo_id_for_full_name(repo["full_name"]),
            )
            for repo in config_result["repos"]
        ]
        daemon_state.start_daemon_run(
            conn,
            config_path=str(config_path),
            owner_id=daemon_run_id,
            now=now,
        )
        daemon_state.record_event(
            conn,
            daemon_run_id,
            "daemon_started",
            "ok",
            {"resource_key": resource_key, "dry_run": dry_run},
            now=now,
        )
    return DaemonContext(
        config_path=str(config_path),
        workflow_path=str(workflow_path) if workflow_path else None,
        db_path=db_path,
        repo_id=repo_ids[0],
        max_concurrency=config_result["max_concurrency"],
        daemon_config=daemon_config,
        daemon_run_id=daemon_run_id,
        lease_id=lease["lease_id"],
        repo_ids=repo_ids,
        dry_run=dry_run,
        startup_live_poll_pending=daemon_config.get("run_on_start", False),
        workflow_engine=config_result.get("workflow_engine"),
    ), None


def _event_result(result):
    """Keep links and bounded diagnostics, not another copy of worker output."""
    payload = {}
    for key in ("ok", "status", "run_id", "cycles", "stop_reason"):
        value = result.get(key)
        if isinstance(value, (str, int, float, bool)):
            payload[key] = value[:200] if isinstance(value, str) else value
    cycles = result.get("cycle_results")
    if isinstance(cycles, list):
        payload["run_ids"] = [
            item["run_id"][:128] for item in cycles[:20]
            if isinstance(item, dict) and isinstance(item.get("run_id"), str)
        ]
    child = result.get("child")
    child = child if isinstance(child, dict) else {}
    if isinstance(child.get("returncode"), int):
        payload["returncode"] = child["returncode"]
    if not result.get("ok", False):
        error = result.get("safe_error") or child.get("stderr")
        if not error and isinstance(cycles, list):
            error = next((item.get("safe_error") for item in cycles
                          if isinstance(item, dict) and item.get("safe_error")), None)
        if error:
            safe = redaction.redact_and_truncate(str(error), 2000)
            payload["safe_error"] = safe.get("text") if safe["ok"] else "[sensitive diagnostic omitted]"
    # Status and identifiers also originate in child output. Fail closed if
    # a malformed response embeds a credential in those otherwise small fields.
    safe = redaction.redact_text(json.dumps(payload, ensure_ascii=False))
    return json.loads(safe["text"]) if safe["ok"] else {"status": "omitted_sensitive"}


def _maybe_prune(context, *, now):
    if not context.daemon_config.get("maintenance_enabled", True):
        return
    current = time.monotonic()
    if current < context.next_maintenance_at:
        return
    config = context.daemon_config
    result = retention.prune_database(
        context.db_path, apply=True, now=now,
        batch_size=config.get("maintenance_batch_size", 100),
        run_retention_days=config.get("run_retention_days", 7),
        failed_run_retention_days=config.get("failed_run_retention_days", 30),
        event_retention_days=config.get("event_retention_days", 7),
    )
    delay = config["local_poll_seconds"] if result["status"] == "busy" else config.get("maintenance_interval_seconds", 300)
    context.next_maintenance_at = current + delay
    if any(result["counts"].values()) or (not result["ok"] and result["status"] != "busy"):
        try:
            with closing(daemon_state.connect(context.db_path)) as conn, conn:
                conn.execute("PRAGMA busy_timeout=100")
                daemon_state.record_event(conn, context.daemon_run_id, "diagnostics_pruned",
                                          "ok" if result["ok"] else "failed", result, now=now)
        except sqlite3.OperationalError:
            # Maintenance must not prevent the next lease renewal or worker poll.
            pass


def _run_local_drain(context, *, skip_publish=False, child_runner=run_child_json, now=None):
    command = build_loop_command(
        context.config_path,
        context.workflow_path,
        dry_run=context.dry_run,
        max_seconds=context.daemon_config["local_drain_timeout_seconds"],
        skip_publish=skip_publish,
    )
    with closing(daemon_state.connect(context.db_path)) as conn, conn:
        daemon_state.record_event(
            conn,
            context.daemon_run_id,
            "local_drain_started",
            "running",
            {"command": command},
            now=now,
        )
    result = child_runner(
        command,
        timeout_seconds=context.daemon_config["local_drain_timeout_seconds"],
    )
    with closing(daemon_state.connect(context.db_path)) as conn, conn:
        daemon_state.record_event(
            conn,
            context.daemon_run_id,
            "local_drain_completed" if result.get("ok") else result.get("status", "child_failed"),
            "ok" if result.get("ok") else "failed",
            _event_result(result),
            now=now,
        )
    return result


def _run_live_poll(context, child_runner=run_child_json, now=None):
    command = build_run_once_command(
        context.config_path,
        context.workflow_path,
        dry_run=context.dry_run,
    )
    with closing(daemon_state.connect(context.db_path)) as conn, conn:
        daemon_state.record_event(
            conn,
            context.daemon_run_id,
            "live_poll_started",
            "running",
            {"command": command},
            now=now,
        )
    result = child_runner(
        command,
        timeout_seconds=context.daemon_config["live_run_timeout_seconds"],
    )
    with closing(daemon_state.connect(context.db_path)) as conn, conn:
        daemon_state.record_event(
            conn,
            context.daemon_run_id,
            "live_poll_completed" if result.get("ok") else result.get("status", "child_failed"),
            "ok" if result.get("ok") else "failed",
            _event_result(result),
            now=now,
        )
    return result


def _rate_limit_decision(context, rate_limit_runner, *, now=None):
    now = now or daemon_state.utc_now()
    cached_decision = context.rate_limit_decision_cache
    cached_until = context.rate_limit_cache_expires_at
    if (
        cached_decision is not None
        and cached_until is not None
        and _parse_time(now) < _parse_time(cached_until)
    ):
        return {**cached_decision, "cached": True}
    rate_limit_result = fetch_rate_limit(runner=rate_limit_runner)
    if not rate_limit_result.get("ok", False):
        decision = {
            "skip": True,
            "reason": "rate_limit_check_failed",
            "rate_limit": rate_limit_result,
        }
    else:
        decision = should_skip_live_poll_for_rate_limit(
            rate_limit_result["rate_limit"],
            context.daemon_config,
        )
    context.rate_limit_decision_cache = decision
    context.rate_limit_cache_expires_at = _iso_after(
        now,
        context.daemon_config["rate_limit_cache_seconds"],
    )
    return {**decision, "cached": False}


def _relay_due_signals(context, *, now):
    mode = (context.workflow_engine or {}).get("mode", "legacy")
    runtime = dbos_runtime.current_runtime()
    if mode not in {"shadow", "active"} or runtime is None or runtime.dbos is None:
        return {
            "ok": True,
            "processed": 0,
            "sent": 0,
            "delivered": 0,
            "failed": 0,
            "retried": 0,
            "attention": 0,
        }
    return workflow_signals.relay_due_signals(
        context.db_path,
        runtime.dbos.send,
        limit=100,
        now=now,
        repo_ids=context.repo_ids or [context.repo_id],
    )


def run_once_decision(
    context,
    *,
    now=None,
    child_runner=run_child_json,
    rate_limit_runner=subprocess.run,
):
    now = now or daemon_state.utc_now()
    _maybe_prune(context, now=now)
    repo_ids = context.repo_ids or [context.repo_id]
    relay_result = _relay_due_signals(context, now=now)
    with closing(daemon_state.connect(context.db_path)) as conn, conn:
        local_work = loop_engine.has_runnable_local_work(conn, repo_ids=repo_ids)
        capacity_full = daemon_state.capacity_full_for_repos(
            conn,
            repo_ids,
            context.max_concurrency,
        )
    live_poll_due = False
    if context.startup_live_poll_pending:
        context.startup_live_poll_pending = False
        live_poll_due = True
    elif context.next_live_poll_at is None:
        _schedule_next_live_poll(context, now=now, capacity_full=capacity_full)
    elif _parse_time(now) >= _parse_time(context.next_live_poll_at):
        live_poll_due = True
    if live_poll_due:
        _schedule_next_live_poll(context, now=now, capacity_full=capacity_full)
        rate_decision = _rate_limit_decision(context, rate_limit_runner, now=now)
        if rate_decision["skip"]:
            with closing(daemon_state.connect(context.db_path)) as conn, conn:
                daemon_state.record_event(
                    conn,
                    context.daemon_run_id,
                    "live_poll_skipped_rate_limit",
                    "skipped",
                    rate_decision,
                    now=now,
                )
            return {
                "ok": True,
                "decision": "live_poll_skipped_rate_limit",
                "rate_limit": rate_decision,
                "relay_result": relay_result,
            }
        live_result = _run_live_poll(context, child_runner=child_runner, now=now)
        drain_result = _run_local_drain(context, child_runner=child_runner, now=now)
        return {
            "ok": bool(live_result.get("ok") and drain_result.get("ok")),
            "decision": "live_poll",
            "live_result": live_result,
            "drain_result": drain_result,
            "relay_result": relay_result,
        }
    if local_work:
        rate_decision = _rate_limit_decision(context, rate_limit_runner, now=now)
        result = _run_local_drain(
            context,
            skip_publish=rate_decision["skip"],
            child_runner=child_runner,
            now=now,
        )
        return {
            "ok": result.get("ok", False),
            "decision": "local_drain",
            "result": result,
            "rate_limit": rate_decision,
            "relay_result": relay_result,
        }
    return {"ok": True, "decision": "idle", "relay_result": relay_result}


def _finish_context(context, status, *, summary=None, error=None, now=None):
    with closing(daemon_state.connect(context.db_path)) as conn, conn:
        daemon_state.release_daemon_lease(conn, context.lease_id, now=now)
        daemon_state.finish_daemon_run(
            conn,
            context.daemon_run_id,
            status,
            summary=summary,
            error=error,
            now=now,
        )


def _heartbeat(context, now=None):
    ttl_seconds = _lease_ttl_seconds(context.daemon_config)
    with closing(daemon_state.connect(context.db_path)) as conn, conn:
        daemon_state.heartbeat_daemon_lease(conn, context.lease_id, ttl_seconds, now=now)



def _resident_loop(
    context,
    *,
    listener_factory=wakeup_notifier.WakeupListener,
    sleeper=time.sleep,
):
    global _STOP_REQUESTED
    _STOP_REQUESTED = False
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    listener = None
    try:
        try:
            listener = listener_factory(context.db_path)
        except OSError as exc:
            with closing(daemon_state.connect(context.db_path)) as conn, conn:
                daemon_state.record_event(
                    conn,
                    context.daemon_run_id,
                    "wakeup_listener_unavailable",
                    "degraded",
                    {"safe_error": str(exc)},
                )
        else:
            with closing(daemon_state.connect(context.db_path)) as conn, conn:
                daemon_state.record_event(
                    conn,
                    context.daemon_run_id,
                    "wakeup_listener_started",
                    "ok",
                    {"path": str(getattr(listener, "path", ""))},
                )
        while not _STOP_REQUESTED:
            result = run_once_decision(context)
            _wait_for_next_decision(
                context,
                result,
                listener=listener,
                sleeper=sleeper,
            )
            _heartbeat(context)
    finally:
        if listener is not None:
            listener.close()
    with closing(daemon_state.connect(context.db_path)) as conn, conn:
        daemon_state.record_event(
            conn,
            context.daemon_run_id,
            "daemon_stopping",
            "ok",
            {},
        )
    _finish_context(context, "stopped", summary={"reason": "signal"})
    return {"ok": True, "status": "stopped", "daemon_run_id": context.daemon_run_id}


def run_daemon(
    config_path,
    workflow_path=None,
    *,
    dry_run=False,
    once=False,
    child_runner=run_child_json,
    rate_limit_runner=subprocess.run,
    now=None,
):
    context, error = _load_context(
        config_path,
        workflow_path=workflow_path,
        dry_run=dry_run,
        now=now,
    )
    if error:
        return error
    runtime = None
    runtime_startup_failed = False
    try:
        workflow_engine = context.workflow_engine or {}
        if workflow_engine.get("mode", "legacy") in {"shadow", "active"}:
            try:
                runtime = dbos_runtime.configure_runtime(
                    {"workflow_engine": workflow_engine}
                )
                dbos_runtime.launch_runtime(runtime)
            except Exception:
                runtime_startup_failed = True
                raise
        if once:
            result = run_once_decision(
                context,
                now=now,
                child_runner=child_runner,
                rate_limit_runner=rate_limit_runner,
            )
            _finish_context(context, "stopped", summary=result, now=now)
            return {**result, "status": "stopped", "daemon_run_id": context.daemon_run_id}
        return _resident_loop(context)
    except Exception as exc:
        error_payload = {
            "status": "failed",
            "safe_error": (
                "DBOS runtime startup failed"
                if runtime_startup_failed
                else str(exc)
            ),
            "error_type": type(exc).__name__,
        }
        _finish_context(context, "failed", error=error_payload, now=now)
        return {"ok": False, **error_payload}
    finally:
        if runtime is not None:
            dbos_runtime.destroy_runtime()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--workflow")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    result = run_daemon(
        args.config,
        workflow_path=args.workflow,
        dry_run=args.dry_run,
        once=args.once,
    )
    return emit(result, 0 if result.get("ok") else 3)


if __name__ == "__main__":
    raise SystemExit(main())
