from robert_agent.work.application import synchronization as synchronization
from robert_agent.work import scan_batches as scan_batches
#!/usr/bin/env python3
import argparse
from contextlib import closing
import json
from pathlib import Path
import time

from robert_agent.foundation.commands import emit
from robert_agent.execution.application import readiness
from robert_agent.execution import journal as run_journal
from robert_agent.readmodels import progress
from robert_agent.work import repositories as work_repositories
from robert_agent.adapters.sqlite import database as storage
from robert_agent.runtime import cycle as run_once_module
from robert_agent.work import queries as work_queries
from robert_agent.configuration import command as validate_config
from robert_agent.foundation import values


def load_config_db_path(config_path, skip_external=False):
    db_path, _repo_ids = load_config_scope(config_path, skip_external=skip_external)
    return db_path


def load_config_scope(config_path, skip_external=False, *, include_config=False):
    config = validate_config.validate_config(config_path, skip_external=skip_external)
    if not config.get("ok"):
        raise ValueError(config.get("safe_error", "invalid config"))
    db_path = Path(config["db_path"])
    storage.init_database(db_path)
    with closing(run_journal.connect(db_path)) as conn:
        repo_ids = [work_repositories.lookup_id(conn, repo["full_name"], fallback=f"repo:{repo['full_name']}")
                    for repo in config["repos"]]
    return (db_path, repo_ids, config) if include_config else (db_path, repo_ids)


def has_runnable_local_work(conn, repo_id=None, repo_ids=None, *, config_result=None, now=None, consume_observations=False, include_publication=True):
    if repo_id is not None and repo_ids is not None:
        raise ValueError("repo_id and repo_ids are mutually exclusive")
    repo_ids = [repo_id] if repo_id else list(repo_ids or [])
    if consume_observations:
        contexts = None
        if config_result and config_result.get('repos'):
            contexts = {}
            for repo in config_result['repos']:
                matched_repo = work_repositories.lookup_id(conn, repo['full_name'])
                if matched_repo and (not repo_ids or matched_repo in repo_ids):
                    known = work_queries.load_known_workstreams(conn, matched_repo)
                    contexts[matched_repo] = synchronization.context_digest(repo, known, config=config_result)
        scoped = repo_ids or work_repositories.list_ids(conn)
        when = values.parse_time(now).timestamp() if now is not None else None
        if scan_batches.has_pending(conn, scoped, now=when, contexts=contexts):
            return True
    return readiness.has_local_work(
        conn, repo_ids, config_result=config_result, now=now, include_publication=include_publication,
    )


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
    with closing(run_journal.connect(db_path)) as conn, conn:
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
        with closing(run_journal.connect(db_path)) as conn, conn:
            before = progress.snapshot_progress(conn)
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
        with closing(run_journal.connect(db_path)) as conn, conn:
            after = progress.snapshot_progress(conn)
            diff = progress.diff_progress(before, after)
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
        **({'failure_scope': cycle_results[-1]['failure_scope']} if cycle_results and cycle_results[-1].get('failure_scope') else {}),
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
