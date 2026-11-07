"""Bounded supervision of existing workers while automatic admission is paused."""

import argparse
from contextlib import closing
import json
import os

from robert_agent.configuration import command as validate_config
from robert_agent.adapters.processes import identity as worker_identity
from robert_agent.execution import leases as execution_leases
from robert_agent.delivery.application.submission import replay_pending_results
from robert_agent.foundation.commands import ControlDeadlineExceeded, emit
from robert_agent.delivery.lock import task_publication_lock
from robert_agent.execution.application import monitor
from robert_agent.foundation import values
from robert_agent.execution import journal as run_journal
from robert_agent.work import repositories as work_repositories, task_state


def run(config_path):
    config = validate_config.validate_config(config_path, skip_external=True)
    if not config.get("ok"):
        return config
    db_path = config["db_path"]
    # Existing durable submissions may ingest while paused. This lane cannot
    # claim a new supervised recovery action on behalf of paused admission.
    recovery_config = {
        **config,
        "supervision": {**config.get("supervision", {}), "mode": "observe"},
    }
    replay_pending_results(db_path, config=recovery_config)
    now = values.utc_now()
    run_id = values.new_id("supervision")
    summary = {
        "executor_id": os.environ.get("_ROBERT_EXECUTOR_ID"),
        "executor_identity": worker_identity.capture_process_identity(os.getpid()),
        "supervision_only": True,
    }
    supervised = 0
    failures = []
    with closing(run_journal.connect(db_path)) as conn:
        with conn:
            run_journal.start_run(conn, run_id, now, config_path, json.dumps(summary))
        try:
            for repo in config["repos"]:
                repo_id = work_repositories.lookup_id(conn, repo["full_name"])
                if repo_id is None:
                    continue
                with conn:
                    lease = execution_leases.acquire(conn, repo_id, run_id, now, config.get("lease_ttl_seconds", 540))
                if not lease["ok"]:
                    continue
                try:
                    # Same task locks as cancellation, result isolation and dispatch.
                    for task_id in task_state.repository_task_ids(conn, repo_id):
                        with task_publication_lock(db_path, task_id) as acquired:
                            if not acquired:
                                continue
                            try:
                                supervised += monitor.supervise_running_attempts(
                                    conn,
                                    repo_id,
                                    config,
                                    values.utc_now(),
                                    allow_recovery=False,
                                    task_id=task_id,
                                    resume_attempt=None,
                                )
                                conn.commit()
                            except ControlDeadlineExceeded:
                                raise
                            except Exception as exc:
                                conn.rollback()
                                failures.append(
                                    {
                                        "repo_id": repo_id,
                                        "task_id": task_id,
                                        "error_type": type(exc).__name__,
                                    }
                                )
                except ControlDeadlineExceeded:
                    conn.rollback()
                    failures.append({"repo_id": repo_id, "error_type": "ControlDeadlineExceeded"})
                    raise
                except Exception as exc:
                    conn.rollback()
                    failures.append({"repo_id": repo_id, "error_type": type(exc).__name__})
                finally:
                    with conn:
                        execution_leases.release(conn, lease["lease_id"], values.utc_now())
        finally:
            with conn:
                summary.update(supervised_count=supervised, failures=failures)
                run_journal.finish_run(
                    conn,
                    run_id,
                    "failed" if failures else "completed",
                    values.utc_now(),
                    summary,
                    sort_keys=False,
                )
    return {
        "ok": not failures,
        "status": "supervised" if not failures else "supervision_failed",
        "run_id": run_id,
        "supervised_count": supervised,
        "failures": failures,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    result = run(args.config)
    return emit(result, 0 if result["ok"] else 3)


if __name__ == "__main__":
    raise SystemExit(main())
