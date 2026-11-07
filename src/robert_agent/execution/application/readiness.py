"""Observe durable readiness without claiming execution or external effects."""

from robert_agent.work import wakeup
from robert_agent.adapters.sqlite import execution_control as execution_store
from robert_agent.execution import retry as retry_policy
from robert_agent.execution import inspection as supervise
from robert_agent.work.application import handoff as pending_handoff
from robert_agent.adapters.processes import attempt as attempt_process
from robert_agent.delivery import results
from robert_agent.execution.application import evidence
from robert_agent.work.application import completion
from robert_agent.execution import capacity
from robert_agent.foundation import values
from robert_agent.work import repositories as work_repositories


def _repo_filter(alias, repo_ids):
    if not repo_ids:
        return "", []
    placeholders = ",".join("?" for _repo_id in repo_ids)
    column = f"{alias}.repo_id" if alias else "repo_id"
    return f" AND {column} IN ({placeholders})", repo_ids


def _supervision_transition_due(conn, repo_ids, config, now):
    """Observe liveness without writing business state or claiming execution."""
    clause, params = _repo_filter("w", repo_ids)
    rows = conn.execute(
        f"""SELECT a.attempt_id,a.task_id,a.status,
        a.heartbeat_at,a.started_at,a.metadata_json,
        EXISTS(SELECT 1 FROM worker_results wr WHERE wr.attempt_id=a.attempt_id)
        FROM attempts a JOIN tasks t ON t.task_id=a.task_id
        JOIN workstreams w ON w.workstream_id=t.workstream_id
        WHERE ({supervise.occupied_attempt_sql()}
               OR (a.status='prepared' AND json_extract(a.metadata_json,'$.result_validation') IS NOT NULL))
          AND t.lifecycle IN ('queued','running')
          AND {execution_store.runnable_sql()} {clause}""",
        params,
    ).fetchall()
    now_dt = values.parse_time(now)
    for (
        attempt_id,
        task_id,
        attempt_status,
        heartbeat_at,
        started_at,
        raw,
        received_result,
    ) in rows:
        metadata = values.json_object(raw)
        if metadata.get("result_validation"):
            return True
        if not heartbeat_at or not started_at:
            if not received_result:
                continue  # The executor cannot classify an incomplete launch yet.
            started_at = started_at or metadata.get("worker_started_at") or now
            heartbeat_at = heartbeat_at or now
        timeout = ((metadata.get("dispatch") or {}).get("worker_launch") or {}).get(
            "timeout_seconds"
        )
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
        if (now_dt - values.parse_time(started_at)).total_seconds() >= config.get(
            "worker_startup_grace_seconds", 300
        ):
            phase = conn.execute(
                "SELECT 1 FROM worker_phases WHERE attempt_id=? LIMIT 1", (attempt_id,)
            ).fetchone()
            if not phase and not evidence.attempt_command_records(
                conn, attempt_id, task_id, persist=False
            ):
                return True
    return False


def _queued_native_round(conn, repo_ids):
    tables = [
        name
        for name in ("review_sessions", "vote_sessions")
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (name,)
        ).fetchone()
    ]
    clause, params = _repo_filter("r", repo_ids)
    for table in tables:
        attention = "".join(
            f""" AND NOT EXISTS (SELECT 1 FROM {other} blocked
            WHERE blocked.workstream_id=rs.workstream_id AND blocked.status='attention')"""
            for other in tables
        )
        if conn.execute(
            f"""SELECT 1 FROM {table} rs JOIN repos r ON r.full_name=rs.repo
            WHERE rs.status='queued' AND rs.task_id IS NULL {clause}
              AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.workstream_id=rs.workstream_id
                AND t.lifecycle IN ('detected','authorized','classified','queued','running','waiting_for_user'))
              AND NOT EXISTS (SELECT 1 FROM {table} old JOIN execution_subjects owner
                ON owner.subject_type='task' AND owner.subject_id=old.task_id
                WHERE old.workstream_id=rs.workstream_id AND owner.binding_status='attention')
              {attention} LIMIT 1""",
            params,
        ).fetchone():
            return True
    return False


def _published_result_needs_finalization(conn, repo_ids):
    # Publisher completion can precede (or outlive) its wakeup. Inspect durable
    # result/action state so a restarted local owner still closes the task.
    clause, params = _repo_filter("w", repo_ids)
    rows = conn.execute(
        f"""SELECT wr.result_id FROM worker_results wr
        JOIN tasks t ON t.task_id=wr.task_id
        JOIN attempts a ON a.attempt_id=wr.attempt_id
        JOIN workstreams w ON w.workstream_id=t.workstream_id
        WHERE t.lifecycle IN ('queued','running') AND a.status='completed'
          AND (json_type(a.metadata_json,'$.dispatch.pid') IS NOT 'integer'
               OR json_extract(a.metadata_json,'$.process_stopped_at') IS NOT NULL)
          AND json_extract(wr.metadata_json,'$.audit.status')='accepted'
          AND {execution_store.runnable_sql()} {clause}""",
        params,
    ).fetchall()
    return any(results.result_actions_all_published(conn, row[0]) for row in rows)


def has_local_work(
    conn, repo_ids, *, config_result=None, now=None, include_publication=True
):
    now = now or wakeup.utc_now()
    if not isinstance(now, str):
        now = now.isoformat()
    wakeup_repo_clause, wakeup_repo_params = _repo_filter("", repo_ids)
    from robert_agent.work import operator_queries as github_operator

    waiting_ids = github_operator.waiting_wakeup_ids(conn, repo_ids)
    waiting_clause = (
        " AND wakeup_id NOT IN (" + ",".join("?" for _ in waiting_ids) + ")"
        if waiting_ids
        else ""
    )
    pending_wakeup = conn.execute(
        f"""
        SELECT 1
        FROM wakeups
        WHERE status = 'pending'
          AND (task_id IS NULL OR (reason='manual_operator_request' AND json_extract(metadata_json,'$.kind')='github_retrigger')
               OR EXISTS (SELECT 1 FROM tasks t WHERE t.task_id=wakeups.task_id AND {execution_store.runnable_sql()}))
          AND COALESCE(json_extract(metadata_json,'$.kind'),'') NOT IN ('dbos_handoff','github_observation_ready','github_context_refresh')
          AND not_before_at <= ?
          AND (expires_at IS NULL OR expires_at > ?)
          AND (? OR reason!='publish_retry_ready')
          {wakeup_repo_clause}
          {waiting_clause}
          AND reason!='worker_result_ready'
        LIMIT 1
        """,
        [now, now, include_publication] + wakeup_repo_params + waiting_ids,
    ).fetchone()
    if pending_wakeup:
        return True
    # Test the exact durable result once for both regular wakeups and lost
    # wakeups. A fenced audit cannot drive repeated empty local cycles.
    from robert_agent.supervision import actions as supervision_actions

    result_clause, result_params = _repo_filter("w", repo_ids)
    unaudited = conn.execute(
        f"""SELECT wr.result_id,wr.task_id,
        json_extract(wr.metadata_json,'$.audit_observation.attempt_count')
        FROM worker_results wr JOIN attempts a ON a.attempt_id=wr.attempt_id
        JOIN tasks t ON t.task_id=wr.task_id JOIN workstreams w ON w.workstream_id=t.workstream_id
        WHERE t.lifecycle IN ('queued','running') AND a.status='completed'
          AND NOT COALESCE({supervise.occupied_attempt_sql()},0)
          AND json_extract(wr.metadata_json,'$.audit') IS NULL AND {execution_store.runnable_sql()}
          AND (json_extract(wr.metadata_json,'$.audit_observation.next_retry_at') IS NULL
               OR julianday(json_extract(wr.metadata_json,'$.audit_observation.next_retry_at'))<=julianday(?))
          {result_clause}""",
        [now, *result_params],
    )
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
    if _queued_native_round(conn, repo_ids) or _published_result_needs_finalization(
        conn, repo_ids
    ):
        return True
    fact_repo_clause, fact_repo_params = _repo_filter("f", repo_ids)
    fact = conn.execute(
        f"""SELECT 1 FROM execution_pending_facts f
      JOIN execution_subjects c ON c.subject_type=f.subject_type AND c.subject_id=f.subject_id
      JOIN tasks t ON t.task_id=f.task_id
      WHERE f.consumed_at IS NULL AND c.binding_status='active'
        AND {execution_store.runnable_sql()}
        AND julianday(f.not_before_at)<=julianday(?) {fact_repo_clause}
        AND (f.subject_type='task' OR f.topic!='remediation_decision' OR EXISTS (
          SELECT 1 FROM pr_remediation_episodes e WHERE e.episode_id=f.subject_id
          AND (e.status IN ('resolved','exhausted','superseded','canceled')
               OR f.dedupe_key!=f.fact_id||':'||e.attempt_count))) LIMIT 1""",
        [now, *fact_repo_params],
    ).fetchone()
    if fact:
        return True
    workstream_repo_clause, workstream_repo_params = _repo_filter("w", repo_ids)
    if _supervision_transition_due(conn, repo_ids, config_result or {}, now):
        return True
    scoped_repos = repo_ids or work_repositories.list_ids(conn)
    if pending_handoff.ready_rows(conn, scoped_repos, now, config=config_result):
        return True
    if completion.has_orphaned_classification_result(conn, scoped_repos, now):
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
          AND (json_extract(a.metadata_json,'$.inspection_retry.next_retry_at') IS NULL
               OR julianday(json_extract(a.metadata_json,'$.inspection_retry.next_retry_at'))<=julianday(?))
          AND c.binding_status='active' AND {execution_store.runnable_sql()}
          AND (a.finished_at IS NULL OR julianday(a.finished_at)<=julianday(?))
          AND a.attempt_no=(SELECT MAX(latest.attempt_no) FROM attempts latest WHERE latest.task_id=t.task_id)
          {workstream_repo_clause} LIMIT 1""",
        [now, now, *workstream_repo_params],
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
          AND json_extract(t.metadata_json,'$.dispatch_wait') IS NULL
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
        global_limit = config_result.get("max_concurrency", 1)
        if capacity.running_attempt_count_for_repos(conn, scoped_repos) < global_limit:
            limits = {
                repo["full_name"]: repo.get("max_concurrency", global_limit)
                for repo in config_result.get("repos", [])
            }
            if any(
                capacity.running_attempt_count(conn, candidate_id)
                < limits.get(full_name, global_limit)
                for candidate_id, full_name in prepared
            ):
                return True
    if not include_publication:
        return False
    publishable = conn.execute(
        f"""
        SELECT 1
        FROM github_actions ga
        JOIN tasks t ON t.task_id = ga.task_id
        JOIN workstreams w ON w.workstream_id = t.workstream_id
        WHERE {retry_policy.publication_ready_sql("ga")}
          AND {execution_store.runnable_sql()}
          {workstream_repo_clause}
        LIMIT 1
        """,
        [now] + workstream_repo_params,
    ).fetchone()
    return bool(publishable)
