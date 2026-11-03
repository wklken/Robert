#!/usr/bin/env python3
import argparse
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

from robert_agent.common import emit
from robert_agent import daemon_state, execution_store, redaction, retention, storage, wakeup_notifier, wakeup, worker_identity
from robert_agent import loop_engine
from robert_agent import validate_config
from robert_agent import notify


SCRIPT_ROOT = Path(__file__).resolve().parent
AGENTS_ROOT = SCRIPT_ROOT.parents[1]
_STOP_REQUESTED = False
_ACTIVE_CHILD_PGID = None
RATE_LIMIT_TIMEOUT_SECONDS = 30
CONTROL_TERM_GRACE_SECONDS = 2
CONTROL_KILL_REAP_SECONDS = 2
# run_once._live_process_group bounds its ps inspection at five seconds.
CONTROL_GROUP_PROOF_SECONDS = 5
STOP_SLEEP_SLICE_SECONDS = 1
LEASE_WAIT_MAX_SLICE_SECONDS = 5
CONTROL_MAX_ATTEMPTS = 3


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
    runtime_config: dict | None = None
    next_health_at: float = 0.0
    publisher_retry_at: float = 0.0
    background_lanes: dict | None = None
    scan_context_refresh: bool = False
    context_refresh_retry_at: float = 0.0
    control_failures: dict = field(default_factory=dict)
    control_retry_at: dict = field(default_factory=dict)
    control_attention: dict = field(default_factory=dict)


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
    consume_observations=False,
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
    if consume_observations:
        command.append("--consume-observations")
    return command


def _short_text(value, max_chars=2000):
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}...[truncated {len(text) - max_chars} chars]"


def _process_group_stopped(pgid):
    """A failed observation cannot prove that executor ownership is releasable."""
    from robert_agent import run_once as executor

    try:
        return not executor._live_process_group(pgid)
    except (OSError, subprocess.TimeoutExpired):
        return False


def _signal_child_group(pgid, signum):
    try:
        os.killpg(pgid, signum)
    except (ProcessLookupError, PermissionError):
        pass  # The owned group already exited.


def _child_stopped_result(command, timeout_seconds, executor_id, group_stopped):
    return {
        "ok": False,
        "status": "child_stopped",
        "safe_error": "daemon stop requested; child was terminated",
        "child": {
            "command": command,
            "timeout_seconds": timeout_seconds,
            "executor_id": executor_id,
            "process_group_stopped": group_stopped,
        },
    }


def run_child_json(command, *, timeout_seconds, runner=subprocess.run):
    global _ACTIVE_CHILD_PGID
    executor_id = None
    group_stopped = False
    try:
        if runner is subprocess.run:
            if _STOP_REQUESTED:
                return _child_stopped_result(command, timeout_seconds, None, True)
            executor_id = uuid4().hex
            margin = min(30, max(.05, timeout_seconds * .1))
            environment = {**os.environ, '_ROBERT_EXECUTOR_ID': executor_id,
                           '_ROBERT_CONTROL_DEADLINE': str(time.monotonic() + max(.01, timeout_seconds - margin))}
            process = subprocess.Popen(command, cwd=str(AGENTS_ROOT), env=environment,
                                       text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
            # The stop handler terminates this group so communicate() returns
            # promptly; Python would otherwise resume the wait after the handler.
            _ACTIVE_CHILD_PGID = process.pid
            if _STOP_REQUESTED:
                _signal_child_group(process.pid, signal.SIGTERM)
            try:
                try:
                    stdout, stderr = process.communicate(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    _signal_child_group(process.pid, signal.SIGTERM)
                    try:
                        process.communicate(timeout=min(CONTROL_TERM_GRACE_SECONDS, max(.1, margin)))
                    except subprocess.TimeoutExpired:
                        _signal_child_group(process.pid, signal.SIGKILL)
                        process.communicate(timeout=CONTROL_KILL_REAP_SECONDS)
                    group_stopped = _process_group_stopped(process.pid)
                    raise
            finally:
                _ACTIVE_CHILD_PGID = None
            if _STOP_REQUESTED:
                # Members that outlived the leader's TERM must not survive the daemon.
                _signal_child_group(process.pid, signal.SIGKILL)
                if process.returncode is not None and process.returncode < 0:
                    group_stopped = _process_group_stopped(process.pid)
                    return _child_stopped_result(command, timeout_seconds, executor_id, group_stopped)
            completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            group_stopped = _process_group_stopped(process.pid)
        else:
            completed = runner(command, cwd=str(AGENTS_ROOT), text=True, capture_output=True,
                               timeout=timeout_seconds, check=False)
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
                "executor_id": executor_id,
                "process_group_stopped": group_stopped,
            },
        }
    stdout = completed.stdout.strip()
    child = {
        "command": command,
        "returncode": completed.returncode,
        "stdout": _short_text(completed.stdout),
        "stderr": _short_text(completed.stderr),
        "executor_id": executor_id,
        "process_group_stopped": group_stopped,
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
        timeout_seconds=RATE_LIMIT_TIMEOUT_SECONDS,
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
    if _ACTIVE_CHILD_PGID is not None:
        _signal_child_group(_ACTIVE_CHILD_PGID, signal.SIGTERM)


def _install_stop_handlers(db_path):
    """Make SIGTERM/SIGINT interrupt the idle wait, not only set a flag."""

    def handler(signum, frame):
        _request_stop(signum, frame)
        # Python retries select() after a handler returns, so wake the
        # listener explicitly; notify() never raises.
        wakeup_notifier.notify(db_path)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def _stop_aware_sleep(seconds):
    deadline = time.monotonic() + max(0, seconds)
    while not _STOP_REQUESTED:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(STOP_SLEEP_SLICE_SECONDS, remaining))


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
    # A live decision performs the rate query, live child, and local child in
    # sequence before the next heartbeat. Each child may need timeout cleanup.
    child_cleanup = (CONTROL_TERM_GRACE_SECONDS + CONTROL_KILL_REAP_SECONDS
                     + CONTROL_GROUP_PROOF_SECONDS)
    return (
        RATE_LIMIT_TIMEOUT_SECONDS
        + 3 * child_cleanup
        + daemon_config["live_run_timeout_seconds"]
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
    if context.background_lanes is not None and context.background_lanes['scan'].process is not None:
        return local_poll_seconds
    if _control_blocked(context, 'scan'):
        return local_poll_seconds
    if context.next_live_poll_at is None:
        return local_poll_seconds
    remaining_seconds = max(
        0,
        (_parse_time(context.next_live_poll_at) - _parse_time(now)).total_seconds(),
    )
    return min(local_poll_seconds, remaining_seconds)


def _sleep_seconds_after_decision(context, result, *, now=None):
    if result.get('decision') == 'control_attention':
        return context.daemon_config['local_poll_seconds']
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
    try:
        with closing(daemon_state.connect(db_path)) as conn:
            execution_store.require_migrated(conn)
    except execution_store.MigrationRequiredError as exc:
        return None, {'ok':False,'status':'migration_required','safe_error':str(exc)}
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
    context = DaemonContext(
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
        runtime_config=config_result,
    )
    _reconcile_abandoned_executors(context)
    return context, None


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
    cause = next((item for item in reversed(cycles or []) if isinstance(item, dict) and not item.get('ok')), result)
    if not result.get('ok'):
        if isinstance(cause.get('status'), str):
            payload['cause_status'] = cause['status'][:200]
        if isinstance(cause.get('required_action'), str):
            payload['required_action'] = cause['required_action'][:2000]
        if isinstance(cause.get('blockers'), list):
            payload['blockers'] = [{key: str(value)[:200] for key, value in item.items()
                if key in {'kind', 'task_id', 'attempt_id', 'result_id'}}
                for item in cause['blockers'][:50] if isinstance(item, dict)]
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


def _finish_stopped_executor(context, executor_id):
    """Only a proven-stopped managed executor may release its repository leases."""
    now = daemon_state.utc_now()
    error = json.dumps({'status': 'executor_interrupted',
                        'safe_error': 'Control executor stopped before recording its outcome.'})
    with closing(daemon_state.connect(context.db_path)) as conn, conn:
        run_ids = [row[0] for row in conn.execute("""SELECT run_id FROM agent_runs
            WHERE status='running' AND json_extract(summary_json,'$.executor_id')=?""", (executor_id,))]
        for run_id in run_ids:
            conn.execute("""UPDATE leases SET status='released',released_at=?,heartbeat_at=?
                WHERE resource_type='agent_run' AND owner_id=? AND status='active'""", (now, now, run_id))
            for table in ('run_steps', 'run_repo_steps'):
                conn.execute(f"""UPDATE {table} SET status=CASE WHEN status='running' THEN 'failed' ELSE 'skipped' END,
                    finished_at=?,error_json=? WHERE run_id=? AND status IN ('running','pending')""", (now, error, run_id))
            conn.execute("""UPDATE agent_runs SET status='failed',finished_at=?,error_json=?
                WHERE run_id=? AND status='running'""", (now, error, run_id))


def _reconcile_child_result(context, result):
    child = result.get('child') or {}
    if child.get('executor_id') and child.get('process_group_stopped'):
        _finish_stopped_executor(context, child['executor_id'])


def _reconcile_abandoned_executors(context):
    with closing(daemon_state.connect(context.db_path)) as conn:
        rows = conn.execute("""SELECT json_extract(summary_json,'$.executor_id'),
            json_extract(summary_json,'$.executor_identity') FROM agent_runs
            WHERE status='running' AND json_type(summary_json,'$.executor_identity')='object'""").fetchall()
    for executor_id, raw in rows:
        identity = json.loads(raw)
        if (not executor_id or identity.get('process_group_id') != identity.get('pid')
                or worker_identity.inspect_recorded_identity(identity) != 'dead'):
            continue
        if _process_group_stopped(identity['pid']):
            _finish_stopped_executor(context, executor_id)


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
        max_batches=3, max_seconds=1,
        run_retention_days=config.get("run_retention_days", 7),
        failed_run_retention_days=config.get("failed_run_retention_days", 30),
        event_retention_days=config.get("event_retention_days", 7),
    )
    delay = config["local_poll_seconds"] if result["status"] == "busy" or result.get('more_work') else config.get("maintenance_interval_seconds", 300)
    context.next_maintenance_at = current + delay
    if result['status'] != 'busy':
        try:
            with closing(daemon_state.connect(context.db_path)) as conn, conn:
                conn.execute('PRAGMA busy_timeout=100')
                wakeup.expire_due_wakeups(conn, now=now)
        except sqlite3.OperationalError:
            pass
    if result['status'] != 'busy' and current >= context.next_health_at:
        context.next_health_at = current + config.get('maintenance_interval_seconds', 300)
        try:
            from robert_agent import reliability_health
            health = reliability_health.collect(context.db_path, now=now, repo_ids=context.repo_ids or [context.repo_id])
            if not health.get('alerts_persisted', True):
                context.next_health_at = current + config['local_poll_seconds']
                context.next_maintenance_at = min(context.next_maintenance_at, context.next_health_at)
            with closing(daemon_state.connect(context.db_path)) as conn, conn:
                conn.execute("PRAGMA busy_timeout=100")
                daemon_state.record_event(conn, context.daemon_run_id, "reliability_health", "ok", health, now=now)
        except sqlite3.OperationalError:
            context.next_health_at = current + config['local_poll_seconds']
            context.next_maintenance_at = min(context.next_maintenance_at, context.next_health_at)
    if any(result["counts"].values()) or (not result["ok"] and result["status"] != "busy"):
        try:
            with closing(daemon_state.connect(context.db_path)) as conn, conn:
                conn.execute("PRAGMA busy_timeout=100")
                daemon_state.record_event(conn, context.daemon_run_id, "diagnostics_pruned",
                                          "ok" if result["ok"] else "failed", result, now=now)
        except sqlite3.OperationalError:
            # Maintenance must not prevent the next lease renewal or worker poll.
            pass


def _supervise_paused_workers(context, *, child_runner=run_child_json, now=None):
    if context.dry_run:
        return {'ok': True, 'status': 'dry_run'}
    from robert_agent import execution_store, supervise
    repo_ids = context.repo_ids or [context.repo_id]
    placeholders = ','.join('?' for _ in repo_ids)
    with closing(daemon_state.connect(context.db_path)) as conn:
        occupied = conn.execute(f"""SELECT 1 FROM attempts a JOIN tasks t ON t.task_id=a.task_id
            JOIN workstreams w ON w.workstream_id=t.workstream_id
            WHERE w.repo_id IN ({placeholders}) AND t.lifecycle IN ('queued','running')
              AND {supervise.occupied_attempt_sql()} AND {execution_store.runnable_sql()} LIMIT 1""", repo_ids).fetchone()
    if not occupied:
        return {'ok': True, 'status': 'idle'}
    result = child_runner([sys.executable, '-B', _script_path('worker_supervision.py'),
                           '--config', str(context.config_path)],
                          timeout_seconds=context.daemon_config['local_drain_timeout_seconds'])
    _reconcile_child_result(context, result)
    # The supervision outcome never clears the failure latch or permits admission.
    if not result.get('ok') or result.get('supervised_count'):
        with closing(daemon_state.connect(context.db_path)) as conn, conn:
            daemon_state.record_event(conn, context.daemon_run_id, 'paused_worker_supervision',
                'ok' if result.get('ok') else 'failed', _event_result(result), now=now)
    return result


def _run_local_drain(context, *, skip_publish=False, child_runner=run_child_json, now=None):
    blocked = _control_blocked(context, 'local')
    if blocked:
        return blocked
    command = build_loop_command(
        context.config_path,
        context.workflow_path,
        dry_run=context.dry_run,
        max_seconds=max(1, int(context.daemon_config["local_drain_timeout_seconds"] * .9)),
        skip_publish=skip_publish,
        consume_observations=context.background_lanes is not None,
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
    _reconcile_child_result(context, result)
    _record_control_outcome(context, 'local', result, now=now)
    with closing(daemon_state.connect(context.db_path)) as conn, conn:
        daemon_state.record_event(
            conn,
            context.daemon_run_id,
            "local_drain_completed" if result.get("ok") else result.get("status", "child_failed"),
            "ok" if result.get("ok") else "failed",
            {**_event_result(result), "control_lane": "local"},
            now=now,
        )
    return result


def _run_live_poll(context, child_runner=run_child_json, now=None):
    blocked = _control_blocked(context, 'scan')
    if blocked:
        return blocked
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
    _reconcile_child_result(context, result)
    _record_control_outcome(context, 'scan', result, now=now)
    with closing(daemon_state.connect(context.db_path)) as conn, conn:
        daemon_state.record_event(
            conn,
            context.daemon_run_id,
            "live_poll_completed" if result.get("ok") else result.get("status", "child_failed"),
            "ok" if result.get("ok") else "failed",
            {**_event_result(result), "control_lane": "scan"},
            now=now,
        )
    return result


def _control_blocked(context, lane):
    if lane in context.control_attention:
        return {**context.control_attention[lane], 'ok': False, 'status': 'control_plane_attention'}
    if time.monotonic() < context.control_retry_at.get(lane, 0):
        return {'ok': True, 'status': 'control_backoff', 'stop_reason': 'no_progress'}
    return None


def _record_control_outcome(context, lane, result, *, now=None):
    """Bound each control lane independently; an attention latch stays resident."""
    quota_wait = (result.get('status') == 'core_rate_limit_floor'
        or bool(result.get('failures')) and all(item.get('reason') == 'core_rate_limit_floor' for item in result['failures']))
    domain_failure = result.get('failure_scope') in {'repository', 'task'}
    if quota_wait or domain_failure:
        # Defer the affected domain without turning healthy controller execution
        # into a permanent lane stop. Real crashes/deadlines still consume budget.
        context.control_failures[lane] = 0
        context.control_retry_at[lane] = time.monotonic() + max(
            context.daemon_config['local_poll_seconds'], int(result.get('retry_after_seconds') or 0))
        return
    if result.get('ok'):
        context.control_failures[lane] = 0
        context.control_retry_at.pop(lane, None)
        if lane in context.control_attention:
            return  # A completed child cannot clear an operator stop or its notice.
        with closing(daemon_state.connect(context.db_path)) as conn, conn:
            conn.execute("""UPDATE notifications SET status='resolved'
                WHERE notification_type='control_plane_failed' AND status!='resolved'
                AND json_extract(metadata_json,'$.control_lane')=?""", (lane,))
        return
    failures = context.control_failures.get(lane, 0) + 1
    context.control_failures[lane] = failures
    context.control_retry_at[lane] = time.monotonic() + context.daemon_config['local_poll_seconds'] * 2 ** (failures - 1)
    diagnostic = _event_result(result)
    permanent = diagnostic.get('cause_status') == 'blocked_result_contract_cutover' or result.get('status') == 'attention'
    if context.control_attention.get(lane, {}).get('cause_status') == 'blocked_result_contract_cutover':
        return  # Preserve the global stop while recording child outcomes separately.
    if not permanent and failures < CONTROL_MAX_ATTEMPTS:
        return
    attention = {'control_lane': lane, 'reason_code': 'control_plane_failed',
        'failure_count': failures, 'retryable': False,
        'required_action': diagnostic.get('required_action') or
            'Automatic retries stopped. Inspect the control failure, repair its cause, then restart the daemon.',
        **diagnostic}
    context.control_attention[lane] = attention
    with closing(daemon_state.connect(context.db_path)) as conn, conn:
        conn.execute("""INSERT OR IGNORE INTO notifications
            (notification_id,notification_type,channel,status,created_at,metadata_json)
            VALUES (?,'control_plane_failed','local','recorded',?,?)""",
            ('notification-control-stop-' + context.daemon_run_id + '-' + lane,
             now or daemon_state.utc_now(), json.dumps(attention)))
        daemon_state.record_event(conn, context.daemon_run_id, 'control_plane_attention', 'attention', attention, now=now)


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


def run_once_decision(
    context,
    *,
    now=None,
    child_runner=run_child_json,
    rate_limit_runner=subprocess.run,
):
    now = now or daemon_state.utc_now()
    from robert_agent import result_cutover
    _poll_background_lanes(context, now=now)
    if (_control_blocked(context, 'local') or any(
            item.get('cause_status') == 'blocked_result_contract_cutover' for item in context.control_attention.values())):
        _supervise_paused_workers(context, child_runner=child_runner, now=now)
    if any(item.get('cause_status') == 'blocked_result_contract_cutover' for item in context.control_attention.values()):
        _maybe_prune(context, now=now)
        if not context.dry_run:
            result_cutover.prepare(context.db_path, now=now, isolated_only=True)
            notify.deliver_pending(context.db_path, now=now, repo_ids=context.repo_ids or [context.repo_id])
        return {'ok': True, 'decision': 'control_attention'}
    cutover = result_cutover.check(context.db_path) if context.dry_run else result_cutover.prepare(context.db_path, now=now)
    if not cutover['ok']:
        _record_control_outcome(context, 'local', cutover, now=now)
        if not context.dry_run:
            notify.deliver_pending(context.db_path, now=now, repo_ids=context.repo_ids or [context.repo_id])
        return {'ok': False, 'decision': 'control_attention', 'result': cutover}
    if context.background_lanes is not None:
        return _background_decision(context, now=now, child_runner=child_runner)
    _maybe_prune(context, now=now)
    if not context.dry_run:
        from robert_agent.worker.result import replay_pending_results
        replay_pending_results(context.db_path)
        notify.deliver_pending(context.db_path, now=now, repo_ids=context.repo_ids or [context.repo_id])
    repo_ids = context.repo_ids or [context.repo_id]
    with closing(daemon_state.connect(context.db_path)) as conn, conn:
        local_work = loop_engine.has_runnable_local_work(
            conn, repo_ids=repo_ids, config_result=context.runtime_config, now=now,
        )
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
                }
        live_result = _run_live_poll(context, child_runner=child_runner, now=now)
        drain_result = _run_local_drain(context, child_runner=child_runner, now=now)
        return {
            "ok": bool(live_result.get("ok") and drain_result.get("ok")),
            "decision": "live_poll",
            "live_result": live_result,
            "drain_result": drain_result,
        }
    if local_work:
        if _control_blocked(context, 'local'):
            return {'ok': True, 'decision': 'control_attention'}
        rate_decision = _rate_limit_decision(context, rate_limit_runner, now=now)
        result = _run_local_drain(
            context,
            skip_publish=bool(rate_decision['skip'] and (
                rate_decision.get('reason') != 'search_rate_limit_floor'
                or (rate_decision.get('rate_limit') or {}).get('core', {}).get('remaining', 0)
                < context.daemon_config['min_core_remaining'])),
            child_runner=child_runner,
            now=now,
        )
        return {
            "ok": result.get("ok", False),
            "decision": "local_drain",
            "result": result,
            "rate_limit": rate_decision,
        }
    return {"ok": True, "decision": "idle"}


def _poll_background_lanes(context, *, now):
    # Admission can pause independently of supervision of children we own.
    for name, lane in (context.background_lanes or {}).items():
        result = lane.poll()
        if result is not None:
            _record_control_outcome(context, name, result, now=now)
            if name == 'publish':
                context.publisher_retry_at = time.monotonic() + (5 if result.get('ok') else 30)
            if name == 'scan' and context.scan_context_refresh:
                context.context_refresh_retry_at = time.monotonic() + max(
                    5 if result.get('ok') else 30, int(result.get('retry_after_seconds') or 0))
                context.scan_context_refresh = False
            elif name == 'scan':
                interval = max(1, int(result.get('poll_interval_seconds') or context.daemon_config['github_poll_seconds']))
                context.next_live_poll_at = _iso_after(now, interval)
            with closing(daemon_state.connect(context.db_path)) as conn, conn:
                daemon_state.record_event(conn, context.daemon_run_id, name + '_completed',
                    'ok' if result.get('ok') else 'failed', result, now=now)


def _background_decision(context, *, now, child_runner=run_child_json):
    from robert_agent import publish as publisher
    from robert_agent.worker.result import replay_pending_results
    _maybe_prune(context, now=now)
    if not context.dry_run:
        replay_pending_results(context.db_path)
    lanes = context.background_lanes
    with closing(daemon_state.connect(context.db_path)) as conn:
        repo_ids = context.repo_ids or [context.repo_id]
        local_work = loop_engine.has_runnable_local_work(conn, repo_ids=repo_ids,
            config_result=context.runtime_config, now=now, consume_observations=True, include_publication=False)
        conn.row_factory = sqlite3.Row
        pending_publish = any(publisher._ready_actions(conn, 1, repo_id=repo_id) for repo_id in repo_ids)
        placeholders = ','.join('?' for _ in repo_ids)
        pending_delivery = bool(os.environ.get('ROBERT_NOTIFICATION_COMMAND') and conn.execute(
            f"""SELECT 1 FROM notifications
                WHERE channel='local' AND status NOT IN ('resolved','sent')
                  AND COALESCE(json_extract(metadata_json,'$.delivery.status'),'') NOT IN ('sent','exhausted')
                  AND (json_extract(metadata_json,'$.delivery.next_retry_at') IS NULL
                       OR julianday(json_extract(metadata_json,'$.delivery.next_retry_at'))<=julianday(?))
                  AND (task_id IS NULL OR task_id IN (
                    SELECT t.task_id FROM tasks t JOIN workstreams w ON w.workstream_id=t.workstream_id
                    WHERE w.repo_id IN ({placeholders}))) LIMIT 1""", [now, *repo_ids]).fetchone())

        refresh_pending = bool(conn.execute(
            f"""SELECT 1 FROM wakeups WHERE repo_id IN ({placeholders}) AND status='pending'
                AND json_extract(metadata_json,'$.kind')='github_context_refresh'
                AND julianday(not_before_at)<=julianday(?)
                AND (expires_at IS NULL OR julianday(expires_at)>julianday(?)) LIMIT 1""",
            [*repo_ids, now, now],
        ).fetchone())
    due = context.startup_live_poll_pending or (context.next_live_poll_at is not None and _parse_time(now) >= _parse_time(context.next_live_poll_at))
    if context.next_live_poll_at is None and not due:
        context.next_live_poll_at = _iso_after(now, context.daemon_config['github_poll_seconds'])
    command = [sys.executable, '-B', _script_path('github_scan.py'), '--config', context.config_path]
    refresh_due = refresh_pending and time.monotonic() >= context.context_refresh_retry_at
    if (due or refresh_due) and lanes['scan'].process is None and not _control_blocked(context, 'scan'):
        context.scan_context_refresh = not due
        if context.scan_context_refresh:
            scan_command = command + ['--refresh-context']
        else:
            scan_command = command
            context.startup_live_poll_pending = False
        lanes['scan'].start(scan_command, timeout_seconds=context.daemon_config['live_run_timeout_seconds'])
    if (pending_publish or pending_delivery) and lanes['publish'].process is None and time.monotonic() >= context.publisher_retry_at and not _control_blocked(context, 'publish'):
        publish_command = command + ['--publish']
        if context.dry_run:
            publish_command.append('--dry-run')
        lanes['publish'].start(publish_command, timeout_seconds=context.daemon_config['local_drain_timeout_seconds'])
    if local_work:
        if _control_blocked(context, 'local'):
            return {'ok': True, 'decision': 'control_attention'}
        result = _run_local_drain(context, skip_publish=True, child_runner=child_runner, now=now)
        return {'ok': result.get('ok', False), 'decision': 'local_drain', 'result': result}
    return {'ok': True, 'decision': 'idle'}


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
    sleeper=_stop_aware_sleep,
):
    global _STOP_REQUESTED
    _STOP_REQUESTED = False
    _install_stop_handlers(context.db_path)
    listener = None
    from robert_agent.control_lane import ControlLane, recover
    if not recover(context.db_path, scope_key=_daemon_resource_key(context.runtime_config), current_owner=context.daemon_run_id):
        _finish_context(context, 'failed', error={'reason_code': 'control_child_attention', 'safe_error': 'Previous control child ownership needs inspection'})
        return {'ok': False, 'status': 'attention', 'safe_error': 'Previous control child ownership needs inspection'}
    context.background_lanes = {name: ControlLane(context.db_path, name, context.daemon_run_id)
                                for name in ('scan', 'publish')}
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
        stopped = [lane.stop() for lane in context.background_lanes.values()]
        cleanup_proven = all(stopped)
        context.background_lanes = None
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
    status = 'stopped' if cleanup_proven else 'attention'
    _finish_context(context, 'stopped' if cleanup_proven else 'failed', summary={'reason': 'signal' if cleanup_proven else 'control_child_attention', 'cleanup_proven': cleanup_proven})
    return {'ok': cleanup_proven, 'status': status, 'daemon_run_id': context.daemon_run_id}


def run_daemon(
    config_path,
    workflow_path=None,
    *,
    dry_run=False,
    once=False,
    wait_for_lease=False,
    child_runner=run_child_json,
    rate_limit_runner=subprocess.run,
    now=None,
):
    global _STOP_REQUESTED
    _STOP_REQUESTED = False
    wait_for_lease = wait_for_lease and not once
    if wait_for_lease:
        _install_stop_handlers(validate_config.validate_config(config_path, skip_external=True).get("db_path") or "")
    while True:
        context, error = _load_context(
            config_path,
            workflow_path=workflow_path,
            dry_run=dry_run,
            now=now,
        )
        if not (wait_for_lease and error and error.get("status") == "skipped_active_daemon"):
            break
        # A killed daemon cannot release its lease. Exiting 0 here makes
        # supervisord give up (FATAL) and systemd never restart, so a service
        # instance waits in-process until the holder heartbeats or it expires.
        expires_at = (error.get("lease") or {}).get("expires_at")
        remaining = (_parse_time(expires_at) - _parse_time(daemon_state.utc_now())).total_seconds() if expires_at else 0
        _stop_aware_sleep(min(LEASE_WAIT_MAX_SLICE_SECONDS, max(STOP_SLEEP_SLICE_SECONDS, remaining)))
        if _STOP_REQUESTED:
            return {"ok": True, "status": "stopped_waiting_for_lease", "lease": error.get("lease")}
    if error:
        return error
    try:
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
            "safe_error": str(exc),
            "error_type": type(exc).__name__,
        }
        _finish_context(context, "failed", error=error_payload, now=now)
        return {"ok": False, **error_payload}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--workflow")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--wait-for-lease", action="store_true",
                        help="wait for another daemon's lease instead of exiting (service managers)")
    args = parser.parse_args(argv)
    result = run_daemon(
        args.config,
        workflow_path=args.workflow,
        dry_run=args.dry_run,
        once=args.once,
        wait_for_lease=args.wait_for_lease,
    )
    return emit(result, 0 if result.get("ok") else 3)


if __name__ == "__main__":
    raise SystemExit(main())
