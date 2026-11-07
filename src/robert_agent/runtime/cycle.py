"""Compose the configured application use cases for a controller cycle."""

from robert_agent.adapters.github import recording as github_recording
from robert_agent.work.application import synchronization as synchronization
from robert_agent.work import scan_batches as scan_batches
from contextlib import closing
from robert_agent.execution.application import dispatch as dispatcher
from pathlib import Path
from robert_agent.adapters.github import discovery as discover
from robert_agent.adapters.processes import identity as worker_identity
from robert_agent.adapters.sqlite import database as storage
from robert_agent.application import repository_cycle
from robert_agent.foundation import errors as run_errors
from robert_agent.execution import journal as run_journal
from robert_agent.execution.application import cutover as result_cutover
from robert_agent.configuration import command as validate_config
from robert_agent.execution import capacity
from robert_agent.adapters.sqlite import execution_control as execution_store
from robert_agent.foundation import values
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.foundation.commands import emit
from robert_agent.work import queries as work_queries
from robert_agent.work import repositories as work_repositories
from robert_agent.adapters.sqlite import review_rounds as review_store
import argparse
import json
import os
import sqlite3
import subprocess


def with_python_bin(repo, config_result):
    return {**repo, "python_bin": config_result["python_bin"]}


def run_once(
    config_path,
    workflow_path=None,
    fixture_path=None,
    dry_run=True,
    skip_external=False,
    skip_publish=False,
    discovery_runner=None,
    max_dispatches=None,
    max_publish_actions=None,
    consume_observations=False,
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
    cutover = result_cutover.check(db_path) if dry_run else result_cutover.prepare(db_path)
    if not cutover["ok"]:
        return cutover
    with closing(sqlite3.connect(db_path)) as pool_conn, pool_conn:
        review_store.configure_process_pool(pool_conn, config_result["model_max_concurrency"])
    with closing(sqlite3.connect(db_path)) as guard:
        execution_store.require_migrated(guard)
    from robert_agent.delivery.application.submission import replay_pending_results

    if not dry_run:
        replay_pending_results(db_path, config=config_result)
    run_id = values.new_id("run")
    try:
        executor_identity = worker_identity.capture_process_identity(os.getpid())
    except (OSError, ValueError, worker_identity.IdentityReadError):
        executor_identity = None
    executor_summary = json.dumps(
        {
            "executor_id": os.environ.get("_ROBERT_EXECUTOR_ID") or values.new_id("executor"),
            "executor_identity": executor_identity,
        }
    )
    run_now = values.utc_now()
    prompt_paths = []
    dispatch_queue = []
    dispatch_results = []
    dispatch_error = None

    with run_journal.run_transaction(db_path, run_id) as conn:
        run_journal.start_run(conn, run_id, run_now, config_path, executor_summary, dry_run=dry_run)
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
            run_journal.insert_run_step(conn, run_id, step)
        run_journal.mark_run_step(
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
            repo = with_python_bin(repo_config, config_result)
            repo_id = work_repositories.upsert_repo(conn, repo)
            repos.append((repo_id, repo))
            repo_ids.append(repo_id)
            run_journal.init_repo_steps(conn, run_id, repo_id)

        notification_buckets = {}
        if not fixture_path and not skip_external:
            run_journal.mark_run_step(conn, run_id, "discover_notifications", "running", run_now)
            conn.commit()
            try:
                notification_buckets = discover.collect_account_notifications(
                    [repo for _repo_id, repo in repos],
                    runner=discovery_runner or subprocess.run,
                )
                run_journal.mark_run_step(
                    conn,
                    run_id,
                    "discover_notifications",
                    "succeeded",
                    run_now,
                    output={"notification_repo_count": len(notification_buckets)},
                )
            except ControlDeadlineExceeded:
                raise
            except (
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                FileNotFoundError,
                json.JSONDecodeError,
            ) as exc:
                error = run_errors.safe_discovery_error(exc)
                run_journal.mark_run_step(
                    conn,
                    run_id,
                    "discover_notifications",
                    "failed",
                    values.utc_now(),
                    error=error,
                )
                notification_buckets = None
        else:
            run_journal.mark_run_step(
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
            max_concurrency - capacity.running_attempt_count_for_repos(conn, repo_ids),
        )
        if max_dispatches is not None:
            capacity_remaining = min(capacity_remaining, max(0, int(max_dispatches)))
        dispatch_budget = {
            "remaining": capacity_remaining,
            "repo_remaining": {
                rid: max(
                    0,
                    int(repo.get("max_concurrency", max_concurrency)) - capacity.running_attempt_count(conn, rid),
                )
                for rid, repo in repos
            },
        }
        publish_budget = {"remaining": (None if max_publish_actions is None else max(0, int(max_publish_actions)))}

        run_journal.mark_run_step(conn, run_id, "repo_pipelines", "running", run_now)
        repo_summaries = []
        for repo_id, repo in repos:
            notification_hints = None
            if notification_buckets is not None:
                notification_hints = notification_buckets.get(repo["full_name"], [])
            observation = None
            repo_discovery_runner = discovery_runner
            if consume_observations:
                known = work_queries.load_known_workstreams(conn, repo_id)
                context = synchronization.context_digest(repo, known, config=config_result)
                observation = scan_batches.next_batch(db_path, repo_id, context=context)
                if observation is None and scan_batches.has_pending(conn, [repo_id]):
                    from robert_agent.work import wakeup as scan_wakeup

                    scan_wakeup.request_wakeup(
                        conn,
                        repo_id=repo_id,
                        reason="manual_operator_request",
                        dedupe_key=context,
                        metadata={"kind": "github_context_refresh"},
                    )
                    conn.commit()
                if observation is not None:
                    repo_discovery_runner = github_recording.ReplayRunner(observation["responses"])
            summary = repository_cycle.run_repo_pipeline(
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
                discovery_runner=repo_discovery_runner,
                notification_hints=notification_hints,
                dispatch_queue=dispatch_queue,
                dispatch_budget=dispatch_budget,
                publish_budget=publish_budget,
                prompt_paths=prompt_paths,
                observation=observation,
                finalize_external=consume_observations,
            )
            conn.commit()
            if consume_observations and observation is not None:
                scan_batches.finish_batch(
                    db_path,
                    observation["batch_id"],
                    success=summary["status"] == "completed",
                    failure_reason=summary["failure_status"] or "local_processing_failed",
                )
            repo_summaries.append(summary)
            if consume_observations and dispatch_queue and dispatch_error is None:
                # The repo transaction and lease are finished. Do not make its
                # prepared tasks wait for another repository's local pipeline.
                try:
                    dispatch_results.extend(
                        dispatcher.dispatch_queued(
                            db_path,
                            repo_ids,
                            dispatch_queue,
                            dry_run,
                            max_concurrency,
                            config_result.get("workers"),
                            config_result.get("default_worker"),
                            config_result,
                        )
                    )
                except Exception as exc:
                    dispatch_error = run_errors.safe_dispatch_error(exc)
                dispatch_queue.clear()
                dispatched = sum(
                    result.get("status") in {"running", "completed_by_worker_result"} for result in dispatch_results
                )
                dispatch_budget["remaining"] = max(
                    0,
                    max_concurrency - capacity.running_attempt_count_for_repos(conn, repo_ids),
                )
                if max_dispatches is not None:
                    dispatch_budget["remaining"] = min(
                        dispatch_budget["remaining"],
                        max(0, max_dispatches - dispatched),
                    )
                dispatch_budget["repo_remaining"] = {
                    rid: max(
                        0,
                        int(candidate.get("max_concurrency", max_concurrency))
                        - capacity.running_attempt_count(conn, rid),
                    )
                    for rid, candidate in repos
                }

        aggregate_counts, publish_result, overall_status = run_journal.summarize_repositories(
            conn,
            run_id,
            run_now,
            repo_summaries,
            len(prompt_paths),
            len(dispatch_queue),
            dry_run=dry_run,
            skip_publish=skip_publish,
        )

        if overall_status == "skipped_active_lease":
            finished_at = values.utc_now()
            for step in ["dispatch", "summarize"]:
                run_journal.mark_run_step(
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
            run_journal.finish_run(conn, run_id, "skipped", finished_at, summary)
            return {
                "ok": True,
                "status": "skipped_active_lease",
                "run_id": run_id,
                "db_path": str(db_path),
                "prompt_paths": [],
                "repo_summaries": repo_summaries,
            }

    try:
        dispatch_results.extend(
            dispatcher.dispatch_queued(
                db_path,
                repo_ids,
                dispatch_queue,
                dry_run,
                max_concurrency,
                config_result.get("workers"),
                config_result.get("default_worker"),
                config_result,
            )
        )
    except Exception as exc:
        dispatch_error = run_errors.safe_dispatch_error(exc)
    finally:
        with closing(run_journal.connect(db_path)) as conn, conn:
            run_journal.finish_dispatch(
                conn,
                run_id,
                overall_status,
                repo_summaries,
                len(prompt_paths),
                dispatch_results,
                dispatch_error,
                aggregate_counts,
                publish_result,
            )
    dispatch_failure = run_journal.dispatch_failure_for(dispatch_results, dispatch_error)
    if dispatch_failure:
        return {
            "ok": False,
            "status": dispatch_failure.get("status", "failed_dispatch"),
            "failure_scope": "task",
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
            "failure_scope": (
                "controller"
                if any(item.get("failure_status") == "repository_pipeline_failed" for item in repo_summaries)
                else "repository"
            ),
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
            failure_summary.get("failure_status") if failure_summary and len(repo_summaries) == 1 else "failed"
        )
        return {
            "ok": False,
            "status": failure_status,
            "failure_scope": (
                "controller"
                if any(item.get("failure_status") == "repository_pipeline_failed" for item in repo_summaries)
                else "repository"
            ),
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
    parser.add_argument("--consume-observations", action="store_true")
    parser.add_argument("--max-dispatches", type=int)
    parser.add_argument("--max-publish-actions", type=int)
    args = parser.parse_args(argv)
    result = run_once(
        args.config,
        workflow_path=args.workflow,
        fixture_path=args.fixture,
        dry_run=args.dry_run,
        skip_external=args.skip_external,
        skip_publish=args.skip_publish,
        max_dispatches=args.max_dispatches,
        max_publish_actions=args.max_publish_actions,
        consume_observations=args.consume_observations,
    )
    return emit(result, 0 if result.get("ok") else 3)


if __name__ == "__main__":
    raise SystemExit(main())
