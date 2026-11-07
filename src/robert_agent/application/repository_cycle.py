"""Run one repository cycle with its existing lease and rollback boundaries."""

from robert_agent.adapters.sqlite import transactions
from robert_agent.foundation import errors as run_errors
from robert_agent.execution import journal as run_journal
from robert_agent.delivery.application import publish
from robert_agent.delivery.application import result_audit
from robert_agent.execution.application import monitor
from robert_agent.work.application import completion
from robert_agent.work.application import discovery
from robert_agent.work.application import intake
from robert_agent.work.application import remediation_requests
from robert_agent.work.application import source_lifecycle
from robert_agent.execution import leases as execution_leases
from robert_agent.execution import notifications as execution_notifications
from robert_agent.foundation import values
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.work import task_state
import subprocess
from robert_agent.work.application import preparation


def _publish_ready_actions_for_repo(
    db_path,
    dry_run,
    repo_id,
    repo_count,
    limit=None,
    config_result=None,
):
    kwargs = {
        "dry_run": dry_run,
        "repo_id": repo_id,
    }
    if limit is not None:
        kwargs["limit"] = limit
    from robert_agent.supervision.application import (
        publication as supervision_publication,
    )

    if supervision_publication.needs_config(config_result):
        kwargs["config"] = config_result
    return publish.publish_ready_actions(db_path, **kwargs)


def run_repo_pipeline(
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
    publish_budget,
    prompt_paths,
    observation=None,
    finalize_external=False,
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
    pipeline_succeeded = False
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
    system_decisions = []
    conn.commit()
    transactions.savepoint(conn, "repo_pipeline")
    try:
        run_journal.mark_repo_run_step(conn, run_id, repo_id, "acquire_lease", "running", run_now)
        lease_result = execution_leases.acquire(
            conn,
            repo_id,
            run_id,
            run_now,
            config_result.get("lease_ttl_seconds", 540),
        )
        summary["lease_status"] = lease_result["status"]
        if not lease_result["ok"]:
            task_state.mark_repository_busy(conn, repo_id, run_now, lease_result.get("expires_at"))
            summary["status"] = lease_result["status"]
            summary["failure_status"] = lease_result["status"]
            run_journal.mark_repo_run_step(
                conn,
                run_id,
                repo_id,
                "acquire_lease",
                "skipped",
                run_now,
                output=lease_result,
            )
            for step in run_journal.REPO_STEP_ORDER[1:]:
                run_journal.mark_repo_run_step(
                    conn,
                    run_id,
                    repo_id,
                    step,
                    "skipped",
                    run_now,
                    output={"reason": lease_result["status"]},
                )
            return summary

        task_state.clear_repository_busy(conn, repo_id)
        run_journal.mark_repo_run_step(
            conn,
            run_id,
            repo_id,
            "acquire_lease",
            "succeeded",
            run_now,
            output=lease_result,
        )

        run_journal.mark_repo_run_step(conn, run_id, repo_id, "supervise", "running", run_now)
        supervised_count = monitor.supervise_running_attempts(
            conn,
            repo_id,
            config_result,
            run_now,
            data_dir=data_dir,
            db_path=db_path,
            repo=repo,
            resume_attempt=preparation.create_resume_attempt_and_prompt,
        )
        failed_timeout_recovered_count = monitor.recover_failed_timeout_attempts(
            conn,
            repo_id,
            run_now,
            data_dir=data_dir,
            db_path=db_path,
            repo=repo,
            resume_attempt=preparation.create_resume_attempt_and_prompt,
        )
        supervised_count += failed_timeout_recovered_count
        summary["supervised_count"] = supervised_count
        summary["failed_timeout_recovered_count"] = failed_timeout_recovered_count
        run_journal.mark_repo_run_step(
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

        run_journal.mark_repo_run_step(conn, run_id, repo_id, "audit_results", "running", run_now)
        try:
            audited_result_count = result_audit.audit_completed_results(
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
                config_result=config_result,
            )
            audited_result_count += completion.recover_orphaned_classification_results(
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
                config_result=config_result,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            error = run_errors.safe_route_error(exc)
            summary["status"] = "failed"
            summary["failure_status"] = error["status"]
            summary["safe_error"] = error["safe_error"]
            run_journal.mark_repo_run_step(
                conn,
                run_id,
                repo_id,
                "audit_results",
                "failed",
                values.utc_now(),
                error=error,
            )
            for step in [
                "publish_actions",
                "reconcile",
                "discover",
                "authorize",
                "route",
            ]:
                run_journal.mark_repo_run_step(
                    conn,
                    run_id,
                    repo_id,
                    step,
                    "skipped",
                    run_now,
                    output={"reason": error["status"]},
                )
            return summary
        audited_result_count += completion.recover_pending_handoffs(
            conn,
            repo_id,
            data_dir,
            db_path,
            repo,
            run_now,
            dry_run,
            dispatch_queue,
            dispatch_budget,
            config_result,
        )
        summary["audited_result_count"] = audited_result_count
        run_journal.mark_repo_run_step(
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
            run_journal.mark_repo_run_step(
                conn,
                run_id,
                repo_id,
                "publish_actions",
                "skipped",
                run_now,
                output=publish_result,
            )
        else:
            run_journal.mark_repo_run_step(conn, run_id, repo_id, "publish_actions", "running", run_now)
            conn.commit()
            publish_limit = publish_budget["remaining"]
            if publish_limit == 0:
                publish_result = {
                    "ok": True,
                    "status": "budget_exhausted",
                    "pending_count": 0,
                    "published_count": 0,
                    "deduplicated_count": 0,
                    "skipped_count": 0,
                    "failed_count": 0,
                }
            else:
                try:
                    publish_result = _publish_ready_actions_for_repo(
                        db_path,
                        dry_run,
                        repo_id,
                        repo_count,
                        limit=publish_limit,
                        config_result=config_result,
                    )
                except Exception as exc:
                    publish_result = run_errors.safe_publish_error(exc)
                if publish_limit is not None:
                    publish_budget["remaining"] = max(
                        0,
                        publish_limit - int(publish_result.get("pending_count", 0)),
                    )
            publish_failed = not publish_result["ok"]
            try:
                finalized_after_publish_count = completion.finalize_published_results(
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
                    config_result=config_result,
                )
            except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                error = run_errors.safe_route_error(exc)
                summary["status"] = "failed"
                summary["failure_status"] = error["status"]
                summary["safe_error"] = error["safe_error"]
                run_journal.mark_repo_run_step(
                    conn,
                    run_id,
                    repo_id,
                    "publish_actions",
                    "failed",
                    values.utc_now(),
                    error=error,
                )
                for step in ["reconcile", "discover", "authorize", "route"]:
                    run_journal.mark_repo_run_step(
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
                run_journal.mark_repo_run_step(
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
                run_journal.mark_repo_run_step(
                    conn,
                    run_id,
                    repo_id,
                    "publish_actions",
                    "succeeded",
                    run_now,
                    output=publish_result,
                )
        if skip_publish and finalize_external:
            publish_result["finalized_task_count"] = completion.finalize_published_results(
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
                config_result=config_result,
            )
        summary["publish_pending_count"] = publish_result.get("pending_count", 0)
        summary["published_count"] = publish_result.get("published_count", 0)
        summary["publish_skipped_count"] = publish_result.get("skipped_count", 0)
        summary["publish_failed_count"] = publish_result.get("failed_count", 0)
        summary["finalized_task_count"] = publish_result.get("finalized_task_count", 0)

        if (fixture_path or skip_external) and observation is None:
            run_journal.mark_repo_run_step(
                conn,
                run_id,
                repo_id,
                "reconcile",
                "skipped",
                run_now,
                output={"reason": "skip_external" if skip_external else "fixture_path"},
            )
        else:
            run_journal.mark_repo_run_step(conn, run_id, repo_id, "reconcile", "running", run_now)
            conn.commit()
            reconciled_source_count = source_lifecycle.reconcile_remote_source_states(
                conn,
                repo_id,
                repo,
                discovery_runner or subprocess.run,
                run_now,
            )
            summary["reconciled_source_count"] = reconciled_source_count
            system_decisions = remediation_requests.collect_pr_remediation_decisions(
                conn,
                repo_id,
                repo,
                discovery_runner or subprocess.run,
                run_now,
            )
            run_journal.mark_repo_run_step(
                conn,
                run_id,
                repo_id,
                "reconcile",
                "succeeded",
                run_now,
                output={"reconciled_source_count": reconciled_source_count},
            )

        discovered = discovery.discover_requests(
            conn,
            repo=repo,
            repo_id=repo_id,
            run_id=run_id,
            run_now=run_now,
            config_result=config_result,
            fixture_path=fixture_path,
            skip_external=skip_external,
            observation=observation,
            discovery_runner=discovery_runner,
            notification_hints=notification_hints,
            system_decisions=system_decisions,
            summary=summary,
        )
        if discovered is None:
            return summary
        decisions, known_workstreams, active_workstreams = discovered

        run_journal.mark_repo_run_step(conn, run_id, repo_id, "route", "running", run_now)
        route_failures = intake.admit_requests(
            conn,
            data_dir=data_dir,
            db_path=db_path,
            repo=repo,
            repo_id=repo_id,
            run_id=run_id,
            run_now=run_now,
            dry_run=dry_run,
            dispatch_queue=dispatch_queue,
            dispatch_budget=dispatch_budget,
            prompt_paths=prompt_paths,
            config_result=config_result,
            decisions=decisions,
            known_workstreams=known_workstreams,
            active_workstreams=active_workstreams,
            summary=summary,
        )

        if route_failures:
            route_error = {**route_failures[0], "failures": route_failures}
            summary["status"] = "failed"
            summary["failure_status"] = route_error["status"]
            summary["safe_error"] = route_error["safe_error"]
            run_journal.mark_repo_run_step(
                conn,
                run_id,
                repo_id,
                "route",
                "failed",
                values.utc_now(),
                output={
                    "prompt_count": len(prompt_paths) - repo_prompt_start,
                    "dispatch_queue_count": len(dispatch_queue) - repo_dispatch_start,
                    "reconciled_source_count": summary["reconciled_source_count"],
                    "current_state_closed_skip_count": summary["current_state_closed_skip_count"],
                },
                error=route_error,
            )
            return summary

        run_journal.mark_repo_run_step(
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
        pipeline_succeeded = True
        return summary
    except ControlDeadlineExceeded:
        transactions.finish_isolation(conn, "repo_pipeline", rollback=True)
        raise
    except Exception as exc:
        failed_step = run_journal.running_repo_step(conn, run_id, repo_id)
        transactions.finish_isolation(conn, "repo_pipeline", rollback=True)
        removed_dispatches = len(dispatch_queue) - repo_dispatch_start
        del dispatch_queue[repo_dispatch_start:]
        dispatch_budget["remaining"] += removed_dispatches
        del prompt_paths[repo_prompt_start:]
        summary.update(
            status="failed",
            failure_status="repository_pipeline_failed",
            safe_error=f"Repository pipeline failed ({type(exc).__name__}).",
        )
        error = {
            "status": summary["failure_status"],
            "safe_error": summary["safe_error"],
            "error_type": type(exc).__name__,
        }
        run_journal.mark_repo_failure(conn, run_id, repo_id, failed_step, run_now, error)
        execution_notifications.update_failure(
            conn,
            "repository_pipeline_failed",
            repo_id,
            run_now,
            {
                "repo_id": repo_id,
                **error,
                "reason_code": "repository_pipeline_failed",
                "required_action": "Inspect the failed repository; other repositories continue.",
            },
        )
        return summary
    finally:
        if lease_result and lease_result.get("ok"):
            execution_leases.release(conn, lease_result["lease_id"], values.utc_now())
        if pipeline_succeeded:
            execution_notifications.update_failure(conn, "repository_pipeline_failed", repo_id, run_now)
        transactions.finish_isolation(conn, "repo_pipeline")
        summary["prompt_count"] = len(prompt_paths) - repo_prompt_start
        summary["dispatch_queue_count"] = len(dispatch_queue) - repo_dispatch_start
