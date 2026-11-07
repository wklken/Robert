"""Observe durable readiness without claiming execution or external effects."""

from robert_agent.adapters.processes import attempt as attempt_process
from robert_agent.delivery import results
from robert_agent.execution import capacity
from robert_agent.execution import inspection as supervise
from robert_agent.execution.application import evidence
from robert_agent.foundation import values
from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import attempts as db_read_attempts
from robert_agent.persistence.queries import (
    execution_pending_facts as db_read_execution_pending_facts,
)
from robert_agent.persistence.queries import github_actions as db_read_github_actions
from robert_agent.persistence.queries import scheduling
from robert_agent.persistence.queries import worker_phases as db_read_worker_phases
from robert_agent.persistence.queries import worker_results as db_read_worker_results
from robert_agent.work import repositories as work_repositories
from robert_agent.work import wakeup
from robert_agent.work.application import completion
from robert_agent.work.application import handoff as pending_handoff


def _supervision_transition_due(conn, repo_ids, config, now):
    """Observe liveness without writing business state or claiming execution."""
    rows = db_read_attempts.supervision_transition_due_filtered(conn, repo_ids)
    now_dt = values.parse_time(now)
    for attempt_id, task_id, attempt_status, heartbeat_at, started_at, raw, received_result in rows:
        metadata = values.json_object(raw)
        if metadata.get("result_validation"):
            return True
        if not heartbeat_at or not started_at:
            if not received_result:
                continue  # The executor cannot classify an incomplete launch yet.
            started_at = started_at or metadata.get("worker_started_at") or now
            heartbeat_at = heartbeat_at or now
        timeout = ((metadata.get("dispatch") or {}).get("worker_launch") or {}).get("timeout_seconds")
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            timeout = None
        if timeout is not None and timeout < 1:
            timeout = None
        status = supervise.classify_attempt(
            heartbeat_at,
            started_at,
            now=now_dt,
            heartbeat_stale_seconds=config.get("heartbeat_stale_seconds", 1200),
            global_timeout_seconds=config.get("hard_timeout_seconds", 2700),
            hard_timeout_seconds=timeout,
        )
        process = attempt_process.dispatch_status(metadata)
        if (
            not received_result
            and process["status"] == "dead"
            and not evidence.inspection_due(metadata.get("inspection_retry", {}), now)
        ):
            continue
        if process["status"] != "alive_same" or status.get("terminate"):
            return True
        if received_result:
            continue  # Only process exit/timeout can finish receipt supervision.
        if (status["status"] == "stale" and attempt_status != "stale") or (
            status["status"] == "running" and attempt_status == "stale"
        ):
            return True
        if (now_dt - values.parse_time(started_at)).total_seconds() >= config.get("worker_startup_grace_seconds", 300):
            phase = db_records.first(db_read_worker_phases.find_for_supervise_running_attempts(conn, attempt_id))
            if not phase and not evidence.attempt_command_records(conn, attempt_id, task_id, persist=False):
                return True
    return False


def _published_result_needs_finalization(conn, repo_ids):
    # Publisher completion can precede (or outlive) its wakeup. Inspect durable
    # result/action state so a restarted local owner still closes the task.
    rows = db_read_worker_results.published_result_needs_finalization_filtered(conn, repo_ids)
    return any(results.result_actions_all_published(conn, row[0]) for row in rows)


def has_local_work(conn, repo_ids, *, config_result=None, now=None, include_publication=True):
    now = now or wakeup.utc_now()
    if not isinstance(now, str):
        now = now.isoformat()
    from robert_agent.work import operator_queries as github_operator

    waiting_ids = github_operator.waiting_wakeup_ids(conn, repo_ids)
    if scheduling.has_pending_local_wakeup(conn, repo_ids, waiting_ids, now, include_publication=include_publication):
        return True
    # Test the exact durable result once for both regular wakeups and lost
    # wakeups. A fenced audit cannot drive repeated empty local cycles.
    from robert_agent.supervision import actions as supervision_actions

    unaudited = db_read_worker_results.has_local_work_filtered(conn, repo_ids, now)
    if any(
        not supervision_actions.blocks_consumer(
            conn,
            task_id=task_id,
            kind="audit_retry",
            target_id=result_id,
            now=now,
            config=config_result,
            native_retry=bool(retries),
        )
        for result_id, task_id, retries in unaudited
    ):
        return True
    if scheduling.has_queued_native_round(conn, repo_ids) or _published_result_needs_finalization(conn, repo_ids):
        return True
    fact = db_records.first(db_read_execution_pending_facts.has_local_work_filtered(conn, repo_ids, now))
    if fact:
        return True
    if _supervision_transition_due(conn, repo_ids, config_result or {}, now):
        return True
    scoped_repos = repo_ids or work_repositories.list_ids(conn)
    if pending_handoff.ready_rows(conn, scoped_repos, now, config=config_result):
        return True
    if completion.has_orphaned_classification_result(conn, scoped_repos, now):
        return True
    recovery_pending = db_records.first(db_read_attempts.has_local_work_filtered(conn, repo_ids, now, now))
    if recovery_pending:
        return True
    prepared = db_read_attempts.has_local_work_filtered_2(conn, repo_ids, now)
    if prepared:
        # Only dispatch-only work waits for capacity. Earlier result, wakeup,
        # observation and supervision checks must still run when the pool is full.
        if config_result is None:
            return True
        global_limit = config_result.get("max_concurrency", 1)
        if capacity.running_attempt_count_for_repos(conn, scoped_repos) < global_limit:
            limits = {
                repo["full_name"]: repo.get("max_concurrency", global_limit) for repo in config_result.get("repos", [])
            }
            if any(
                capacity.running_attempt_count(conn, candidate_id) < limits.get(full_name, global_limit)
                for candidate_id, full_name in prepared
            ):
                return True
    if not include_publication:
        return False
    publishable = db_records.first(db_read_github_actions.has_local_work_filtered(conn, repo_ids, now))
    return bool(publishable)
