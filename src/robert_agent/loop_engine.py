#!/usr/bin/env python3
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

from robert_agent.common import emit
from robert_agent import storage, wakeup
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


def load_config_scope(config_path, skip_external=False):
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
    return db_path, repo_ids


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


def has_runnable_local_work(conn, repo_id=None, repo_ids=None):
    if repo_id is not None and repo_ids is not None:
        raise ValueError("repo_id and repo_ids are mutually exclusive")
    repo_ids = [repo_id] if repo_id else list(repo_ids or [])
    now = wakeup.utc_now()
    wakeup.expire_due_wakeups(conn, now=now)
    wakeup_repo_clause, wakeup_repo_params = _repo_filter("", repo_ids)
    pending_wakeup = conn.execute(
        f"""
        SELECT 1
        FROM wakeups
        WHERE status = 'pending'
          AND not_before_at <= ?
          AND (expires_at IS NULL OR expires_at > ?)
          {wakeup_repo_clause}
        LIMIT 1
        """,
        [now, now] + wakeup_repo_params,
    ).fetchone()
    if pending_wakeup:
        return True
    workstream_repo_clause, workstream_repo_params = _repo_filter("w", repo_ids)
    supervise_needed = conn.execute(
        f"""
        SELECT 1
        FROM attempts a
        JOIN tasks t ON t.task_id = a.task_id
        JOIN workstreams w ON w.workstream_id = t.workstream_id
        WHERE a.status IN ('running', 'stale')
          AND t.lifecycle IN ('queued', 'running')
          {workstream_repo_clause}
        LIMIT 1
        """,
        workstream_repo_params,
    ).fetchone()
    if supervise_needed:
        return True
    prepared = conn.execute(
        f"""
        SELECT 1
        FROM attempts a
        JOIN tasks t ON t.task_id = a.task_id
        JOIN workstreams w ON w.workstream_id = t.workstream_id
        WHERE a.status = 'prepared'
          AND t.lifecycle IN ('queued', 'running')
          {workstream_repo_clause}
        LIMIT 1
        """,
        workstream_repo_params,
    ).fetchone()
    if prepared:
        return True
    publishable = conn.execute(
        f"""
        SELECT 1
        FROM github_actions ga
        JOIN tasks t ON t.task_id = ga.task_id
        JOIN workstreams w ON w.workstream_id = t.workstream_id
        WHERE ga.audit_status = 'accepted'
          AND ga.publish_status = 'not_published'
          {workstream_repo_clause}
        LIMIT 1
        """,
        workstream_repo_params,
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
):
    started = time.monotonic()
    db_path, repo_ids = load_config_scope(config_path, skip_external=skip_external)
    cycle_results = []
    stop_reason = "max_cycles"
    with closing(_connect(db_path)) as conn, conn:
        if not has_runnable_local_work(conn, repo_ids=repo_ids):
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
                    "max_publish_actions_enforcement": "not_enforced",
                },
                "db_path": str(db_path),
            }
            summary["latest_loop_path"] = _write_latest_loop(db_path, summary)
            return summary

    for cycle in range(max_cycles):
        if time.monotonic() - started >= max_seconds:
            stop_reason = "max_seconds"
            break
        with closing(_connect(db_path)) as conn, conn:
            before = snapshot_progress(conn)
        result = runner(
            config_path,
            workflow_path=workflow_path,
            dry_run=dry_run,
            skip_external=skip_external,
            skip_publish=skip_publish,
            max_dispatches=max_dispatches,
        )
        cycle_results.append(result)
        if not result.get("ok"):
            stop_reason = "run_once_failed"
            break
        with closing(_connect(db_path)) as conn, conn:
            after = snapshot_progress(conn)
            diff = diff_progress(before, after)
            runnable = has_runnable_local_work(conn, repo_ids=repo_ids)
        cycle_results[-1] = {**result, "progress": diff}
        if not diff["changed"]:
            stop_reason = "no_progress"
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
            "max_publish_actions_enforcement": "not_enforced",
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
