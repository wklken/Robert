#!/usr/bin/env python3
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

from robert_agent.common import emit
from robert_agent import storage, wakeup, execution_store, retry_policy, supervise
from robert_agent import run_once as run_once_module
from robert_agent import validate_config


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def load_config_db_path(config_path, skip_external=False):
    db_path, _repo_ids = load_config_scope(config_path, skip_external=skip_external)
    return db_path


def load_config_scope(config_path, skip_external=False, *, include_config=False):
    config = validate_config.validate_config(config_path, skip_external=skip_external)
    if not config.get("ok"):
        raise ValueError(config.get("safe_error", "invalid config"))
    db_path = Path(config["db_path"])
    storage.init_database(db_path)
    with closing(_connect(db_path)) as conn:
        repo_ids = []
        for repo in config["repos"]:
            full_name = repo["full_name"]
            row = conn.execute(
                "SELECT repo_id FROM repos WHERE full_name = ?",
                (full_name,),
            ).fetchone()
            repo_ids.append(row[0] if row else f"repo:{full_name}")
    return (db_path, repo_ids, config) if include_config else (db_path, repo_ids)


def _count(conn, table, where="1 = 1", params=()):
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {where}",
        params,
    ).fetchone()[0]


def snapshot_progress(conn):
    return {
        "pending_wakeups": _count(conn, "wakeups", "status = 'pending'"),
        "consumed_wakeups": _count(conn, "wakeups", "status = 'consumed'"),
        "expired_wakeups": _count(conn, "wakeups", "status = 'expired'"),
        "prepared_attempts": _count(conn, "attempts", "status = 'prepared'"),
        "running_attempts": _count(conn, "attempts", "status = 'running'"),
        "stale_attempts": _count(conn, "attempts", "status = 'stale'"),
        "completed_attempts": _count(conn, "attempts", "status = 'completed'"),
        "failed_attempts": _count(conn, "attempts", "status = 'failed'"),
        "pending_actions": _count(conn, "github_actions", "audit_status = 'pending'"),
        "accepted_unpublished_actions": _count(
            conn,
            "github_actions",
            "audit_status = 'accepted' AND publish_status = 'not_published'",
        ),
        "published_actions": _count(conn, "github_actions", "publish_status = 'published'"),
        "running_tasks": _count(conn, "tasks", "lifecycle IN ('queued', 'running')"),
        "completed_tasks": _count(conn, "tasks", "lifecycle = 'completed'"),
        "failed_tasks": _count(conn, "tasks", "lifecycle = 'failed'"),
        "active_workstreams": _count(conn, "workstreams", "lifecycle = 'active'"),
        "waiting_workstreams": _count(conn, "workstreams", "lifecycle = 'waiting_for_user'"),
        "completed_workstreams": _count(conn, "workstreams", "lifecycle = 'completed'"),
    }


def diff_progress(before, after):
    changed = {
        key: {"before": before.get(key), "after": after.get(key)}
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    }
    return {"changed": bool(changed), "changed_fields": changed}


def _repo_filter(alias, repo_ids):
    if not repo_ids:
        return "", []
    placeholders = ",".join("?" for _repo_id in repo_ids)
    column = f"{alias}.repo_id" if alias else "repo_id"
    return f" AND {column} IN ({placeholders})", repo_ids


def _supervision_transition_due(conn, repo_ids, config, now):
    """Observe liveness without writing business state or claiming execution."""
    clause, params = _repo_filter("w", repo_ids)
    rows = conn.execute(f"""SELECT a.attempt_id,a.task_id,a.status,
        a.heartbeat_at,a.started_at,a.metadata_json,
        EXISTS(SELECT 1 FROM worker_results wr WHERE wr.attempt_id=a.attempt_id)
        FROM attempts a JOIN tasks t ON t.task_id=a.task_id
        JOIN workstreams w ON w.workstream_id=t.workstream_id
        WHERE ({supervise.occupied_attempt_sql()}
               OR (a.status='prepared' AND json_extract(a.metadata_json,'$.result_validation') IS NOT NULL))
          AND t.lifecycle IN ('queued','running')
          AND {execution_store.runnable_sql()} {clause}""", params).fetchall()
    now_dt = run_once_module._parse_time(now)
    for attempt_id, task_id, attempt_status, heartbeat_at, started_at, raw, received_result in rows:
        metadata = run_once_module._json_object(raw)
        if metadata.get('result_validation'):
            return True
        if not heartbeat_at or not started_at:
            if not received_result:
                continue  # The executor cannot classify an incomplete launch yet.
            started_at = started_at or metadata.get('worker_started_at') or now
            heartbeat_at = heartbeat_at or now
        timeout = ((metadata.get('dispatch') or {}).get('worker_launch') or {}).get('timeout_seconds')
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            timeout = None
        if timeout is not None and timeout < 1:
            timeout = None
        status = supervise.classify_attempt(
            heartbeat_at, started_at, now=now_dt,
            stale_after_minutes=config.get('stale_after_minutes', 20),
            hard_timeout_minutes=config.get('hard_timeout_minutes', 45),
            hard_timeout_seconds=timeout,
        )
        process = run_once_module._dispatch_process_status(metadata)
        if process['status'] != 'alive_same' or status.get('terminate'):
            return True
        if received_result:
            continue  # Only process exit/timeout can finish receipt supervision.
        if ((status['status'] == 'stale' and attempt_status != 'stale')
                or (status['status'] == 'running' and attempt_status == 'stale')):
            return True
        if (now_dt - run_once_module._parse_time(started_at)).total_seconds() >= config.get('worker_startup_grace_seconds', 300):
            phase = conn.execute('SELECT 1 FROM worker_phases WHERE attempt_id=? LIMIT 1',
                                 (attempt_id,)).fetchone()
            if not phase and not run_once_module._attempt_command_records(
                    conn, attempt_id, task_id, persist=False):
                return True
    return False


def _queued_native_round(conn, repo_ids):
    tables = [name for name in ('review_sessions', 'vote_sessions')
              if conn.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (name,)).fetchone()]
    clause, params = _repo_filter('r', repo_ids)
    for table in tables:
        attention = ''.join(f""" AND NOT EXISTS (SELECT 1 FROM {other} blocked
            WHERE blocked.workstream_id=rs.workstream_id AND blocked.status='attention')"""
                            for other in tables)
        if conn.execute(f"""SELECT 1 FROM {table} rs JOIN repos r ON r.full_name=rs.repo
            WHERE rs.status='queued' AND rs.task_id IS NULL {clause}
              AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.workstream_id=rs.workstream_id
                AND t.lifecycle IN ('detected','authorized','classified','queued','running','waiting_for_user'))
              AND NOT EXISTS (SELECT 1 FROM {table} old JOIN execution_subjects owner
                ON owner.subject_type='task' AND owner.subject_id=old.task_id
                WHERE old.workstream_id=rs.workstream_id AND owner.binding_status='attention')
              {attention} LIMIT 1""", params).fetchone():
            return True
    return False


def _published_result_needs_finalization(conn, repo_ids):
    # Publisher completion can precede (or outlive) its wakeup. Inspect durable
    # result/action state so a restarted local owner still closes the task.
    clause, params = _repo_filter('w', repo_ids)
    rows = conn.execute(f"""SELECT wr.result_id FROM worker_results wr
        JOIN tasks t ON t.task_id=wr.task_id
        JOIN attempts a ON a.attempt_id=wr.attempt_id
        JOIN workstreams w ON w.workstream_id=t.workstream_id
        WHERE t.lifecycle IN ('queued','running') AND a.status='completed'
          AND (json_type(a.metadata_json,'$.dispatch.pid') IS NOT 'integer'
               OR json_extract(a.metadata_json,'$.process_stopped_at') IS NOT NULL)
          AND json_extract(wr.metadata_json,'$.audit.status')='accepted'
          AND {execution_store.runnable_sql()} {clause}""", params).fetchall()
    return any(run_once_module._result_actions_all_published(conn, row[0]) for row in rows)


def has_runnable_local_work(conn, repo_id=None, repo_ids=None, *, config_result=None, now=None, consume_observations=False, include_publication=True):
    if repo_id is not None and repo_ids is not None:
        raise ValueError("repo_id and repo_ids are mutually exclusive")
    repo_ids = [repo_id] if repo_id else list(repo_ids or [])
    if consume_observations:
        from robert_agent import github_scan
        contexts = None
        if config_result and config_result.get('repos'):
            contexts = {}
            for repo in config_result['repos']:
                row = conn.execute('SELECT repo_id FROM repos WHERE full_name=?', (repo['full_name'],)).fetchone()
                if row and (not repo_ids or row[0] in repo_ids):
                    known = run_once_module._load_known_workstreams(conn, row[0])
                    contexts[row[0]] = github_scan.context_digest(repo, known, config=config_result)
        scoped = repo_ids or [row[0] for row in conn.execute('SELECT repo_id FROM repos')]
        when = run_once_module._parse_time(now).timestamp() if now is not None else None
        if github_scan.has_pending(conn, scoped, now=when, contexts=contexts):
            return True
    now = now or wakeup.utc_now()
    if not isinstance(now, str):
        now = now.isoformat()
    wakeup_repo_clause, wakeup_repo_params = _repo_filter("", repo_ids)
    pending_wakeup = conn.execute(
        f"""
        SELECT 1
        FROM wakeups
        WHERE status = 'pending'
          AND COALESCE(json_extract(metadata_json,'$.kind'),'') NOT IN ('dbos_handoff','github_observation_ready','github_context_refresh')
          AND not_before_at <= ?
          AND (expires_at IS NULL OR expires_at > ?)
          AND (? OR reason!='publish_retry_ready')
          {wakeup_repo_clause}
          AND (reason!='worker_result_ready' OR EXISTS (
            SELECT 1 FROM worker_results wr JOIN attempts a ON a.attempt_id=wr.attempt_id
            JOIN tasks t ON t.task_id=a.task_id
            WHERE (wr.result_id=wakeups.result_id OR a.attempt_id=wakeups.attempt_id)
              AND a.status='completed' AND NOT COALESCE({supervise.occupied_attempt_sql()},0)
              AND t.lifecycle IN ('queued','running') AND {execution_store.runnable_sql()}))
        LIMIT 1
        """,
        [now, now, include_publication] + wakeup_repo_params,
    ).fetchone()
    if pending_wakeup or _queued_native_round(conn, repo_ids) or _published_result_needs_finalization(conn, repo_ids):
        return True
    fact_repo_clause,fact_repo_params=_repo_filter('f',repo_ids)
    fact=conn.execute(f"""SELECT 1 FROM execution_pending_facts f
      JOIN execution_subjects c ON c.subject_type=f.subject_type AND c.subject_id=f.subject_id
      WHERE f.consumed_at IS NULL AND c.binding_status='active'
        AND julianday(f.not_before_at)<=julianday(?) {fact_repo_clause}
        AND (f.subject_type='task' OR f.topic!='remediation_decision' OR EXISTS (
          SELECT 1 FROM pr_remediation_episodes e WHERE e.episode_id=f.subject_id
          AND (e.status IN ('resolved','exhausted','superseded','canceled')
               OR f.dedupe_key!=f.fact_id||':'||e.attempt_count))) LIMIT 1""",[now,*fact_repo_params]).fetchone()
    if fact:
        return True
    workstream_repo_clause, workstream_repo_params = _repo_filter("w", repo_ids)
    if _supervision_transition_due(conn, repo_ids, config_result or {}, now):
        return True
    scoped_repos = repo_ids or [row[0] for row in conn.execute('SELECT repo_id FROM repos')]
    if run_once_module.has_orphaned_classification_result(conn, scoped_repos, now):
        return True
    recovery_pending = conn.execute(
        f"""SELECT 1 FROM attempts a
        JOIN tasks t ON t.task_id=a.task_id
        JOIN workstreams w ON w.workstream_id=t.workstream_id
        JOIN execution_subjects c ON
          (c.subject_type='task' AND c.subject_id=t.task_id) OR
          (c.subject_type='remediation_episode' AND c.subject_id=json_extract(t.metadata_json,'$.remediation_episode_id'))
        WHERE a.status='failed' AND t.lifecycle='failed' AND w.lifecycle='failed'
          AND json_extract(a.failure_json,'$.status')='failed_timeout'
          AND c.binding_status='active' AND {execution_store.runnable_sql()}
          AND (a.finished_at IS NULL OR julianday(a.finished_at)<=julianday(?))
          AND a.attempt_no=(SELECT MAX(latest.attempt_no) FROM attempts latest WHERE latest.task_id=t.task_id)
          {workstream_repo_clause} LIMIT 1""",
        [now, *workstream_repo_params],
    ).fetchone()
    if recovery_pending:
        return True
    prepared = conn.execute(
        f"""
        SELECT DISTINCT w.repo_id, r.full_name
        FROM attempts a
        JOIN tasks t ON t.task_id = a.task_id
        JOIN workstreams w ON w.workstream_id = t.workstream_id
        JOIN repos r ON r.repo_id = w.repo_id
        WHERE a.status = 'prepared'
          AND (json_extract(a.metadata_json,'$.resume.not_before_at') IS NULL
               OR julianday(json_extract(a.metadata_json,'$.resume.not_before_at'))<=julianday(?))
          AND t.lifecycle IN ('queued', 'running')
          AND {execution_store.runnable_sql()}
          {workstream_repo_clause}
        """,
        [now, *workstream_repo_params],
    ).fetchall()
    if prepared:
        # Only dispatch-only work waits for capacity. Earlier result, wakeup,
        # observation and supervision checks must still run when the pool is full.
        if config_result is None:
            return True
        global_limit = config_result.get('max_concurrency', 1)
        if run_once_module._running_attempt_count_for_repos(conn, scoped_repos) < global_limit:
            limits = {repo['full_name']: repo.get('max_concurrency', global_limit)
                      for repo in config_result.get('repos', [])}
            if any(run_once_module._running_attempt_count(conn, candidate_id)
                   < limits.get(full_name, global_limit) for candidate_id, full_name in prepared):
                return True
    if not include_publication:
        return False
    publishable = conn.execute(
        f"""
        SELECT 1
        FROM github_actions ga
        JOIN tasks t ON t.task_id = ga.task_id
        JOIN workstreams w ON w.workstream_id = t.workstream_id
        WHERE {retry_policy.publication_ready_sql('ga')}
          AND {execution_store.runnable_sql()}
          {workstream_repo_clause}
        LIMIT 1
        """,
        [now] + workstream_repo_params,
    ).fetchone()
    return bool(publishable)


def _write_latest_loop(db_path, summary):
    path = Path(db_path).parent / "latest-loop.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return str(path)


def run_loop(
    config_path,
    workflow_path=None,
    dry_run=True,
    skip_external=False,
    skip_publish=False,
    max_cycles=3,
    max_seconds=120,
    max_dispatches=2,
    max_publish_actions=5,
    runner=run_once_module.run_once,
    consume_observations=False,
):
    started = time.monotonic()
    if max_publish_actions < 0:
        raise ValueError("max_publish_actions must be at least 0")
    db_path, repo_ids, config = load_config_scope(
        config_path, skip_external=skip_external, include_config=True)
    cycle_results = []
    stop_reason = "max_cycles"
    publish_actions_used = 0
    with closing(_connect(db_path)) as conn, conn:
        if not has_runnable_local_work(conn, repo_ids=repo_ids, config_result=config,
                consume_observations=consume_observations, include_publication=not skip_publish):
            summary = {
                "ok": True,
                "status": "completed",
                "cycles": 0,
                "stop_reason": "no_runnable_work",
                "cycle_results": [],
                "budgets": {
                    "max_cycles": max_cycles,
                    "max_seconds": max_seconds,
                    "max_dispatches": max_dispatches,
                    "max_publish_actions": max_publish_actions,
                    "max_publish_actions_used": publish_actions_used,
                    "max_publish_actions_enforcement": "enforced",
                },
                "db_path": str(db_path),
            }
            summary["latest_loop_path"] = _write_latest_loop(db_path, summary)
            return summary

    for cycle in range(max_cycles):
        if time.monotonic() - started >= max_seconds:
            stop_reason = "max_seconds"
            break
        remaining_publish_actions = max_publish_actions - publish_actions_used
        if not skip_publish and remaining_publish_actions <= 0:
            stop_reason = "max_publish_actions"
            break
        with closing(_connect(db_path)) as conn, conn:
            before = snapshot_progress(conn)
        observation_kwargs = {'consume_observations': True} if consume_observations else {}
        result = runner(
            config_path,
            workflow_path=workflow_path,
            dry_run=dry_run,
            skip_external=skip_external,
            skip_publish=skip_publish,
            max_dispatches=max_dispatches,
            max_publish_actions=remaining_publish_actions,
            **observation_kwargs,
        )
        cycle_results.append(result)
        if not result.get("ok"):
            stop_reason = "run_once_failed"
            break
        publish_actions_used += min(
            remaining_publish_actions,
            max(
                0,
                int((result.get("publish_result") or {}).get("pending_count", 0)),
            ),
        )
        with closing(_connect(db_path)) as conn, conn:
            after = snapshot_progress(conn)
            diff = diff_progress(before, after)
            runnable = has_runnable_local_work(conn, repo_ids=repo_ids, config_result=config,
                consume_observations=consume_observations, include_publication=not skip_publish)
        cycle_results[-1] = {**result, "progress": diff}
        if not diff["changed"]:
            stop_reason = "no_progress"
            break
        if not skip_publish and publish_actions_used >= max_publish_actions:
            stop_reason = "max_publish_actions"
            break
        if not runnable:
            stop_reason = "no_runnable_work"
            break
    else:
        stop_reason = "max_cycles"
    summary = {
        "ok": stop_reason != "run_once_failed",
        "status": "completed" if stop_reason != "run_once_failed" else "failed",
        "cycles": len(cycle_results),
        "stop_reason": stop_reason,
        "cycle_results": cycle_results,
        "budgets": {
            "max_cycles": max_cycles,
            "max_seconds": max_seconds,
            "max_dispatches": max_dispatches,
            "max_publish_actions": max_publish_actions,
            "max_publish_actions_used": publish_actions_used,
            "max_publish_actions_enforcement": "enforced",
        },
        "db_path": str(db_path),
    }
    summary["latest_loop_path"] = _write_latest_loop(db_path, summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--workflow")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-external", action="store_true")
    parser.add_argument("--skip-publish", action="store_true")
    parser.add_argument("--consume-observations", action="store_true")
    parser.add_argument("--max-cycles", type=int, default=3)
    parser.add_argument("--max-seconds", type=int, default=120)
    parser.add_argument("--max-dispatches", type=int, default=2)
    parser.add_argument("--max-publish-actions", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        result = run_loop(
            args.config,
            workflow_path=args.workflow,
            dry_run=args.dry_run,
            skip_external=args.skip_external,
            skip_publish=args.skip_publish,
            consume_observations=args.consume_observations,
            max_cycles=args.max_cycles,
            max_seconds=args.max_seconds,
            max_dispatches=args.max_dispatches,
            max_publish_actions=args.max_publish_actions,
        )
    except ValueError as exc:
        return emit({"ok": False, "status": "failed_config", "safe_error": str(exc)}, exit_code=3)
    return emit(result, 0 if result.get("ok") else 3)


if __name__ == "__main__":
    raise SystemExit(main())
