"""Shared lifecycle facts and safe operator recovery for a work item."""

from robert_agent.adapters.processes import attempt as attempt_process
import json

from robert_agent.delivery.lock import task_publication_lock


def require_idle_repository(conn, repo_id, now):
    from robert_agent.work.errors import WorkItemConflictError

    busy = conn.execute(
        """SELECT 1 FROM leases WHERE resource_type='agent_run' AND resource_key=?
        AND status='active' AND expires_at>? LIMIT 1""",
        (repo_id, now),
    ).fetchone()
    if busy:
        raise WorkItemConflictError(
            "The repository is processing a cycle. Refresh and retry after it finishes."
        )


def cancel_execution(
    conn, item, facts, now, db_path, locks, *, abandon_review_run_ids=()
):
    from robert_agent.work.errors import WorkItemConflictError

    # The write transaction prevents a new engine lease. Publication has its
    # own task lock, held until this command commits; neither lock waits on I/O.
    require_idle_repository(conn, item["repo_id"], now)
    placeholders = ",".join("?" for _ in facts["streams"]) or "NULL"
    task_ids = conn.execute(
        f"SELECT task_id FROM tasks WHERE workstream_id IN ({placeholders}) ORDER BY task_id",
        facts["streams"],
    )
    task_ids = list(task_ids)
    for (task_id,) in task_ids:
        if not locks.enter_context(task_publication_lock(db_path, task_id)):
            raise WorkItemConflictError(
                "A publication is in progress for this task. Retry cancellation after it finishes."
            )
    from robert_agent.execution import rounds as native_rounds

    unknown = {}
    for (task_id,) in task_ids:
        round_store, review = native_rounds.owner(conn, task_id)
        if review:
            unknown.update(
                {
                    run["run_id"]: {**run, "store": round_store}
                    for run in round_store.runs(conn, review["round_id"])
                    if round_store.can_abandon(run)
                }
            )
    if set(abandon_review_run_ids) - unknown.keys():
        raise WorkItemConflictError(
            "The selected unknown review launch changed. Refresh and inspect before abandoning it."
        )
    for attempt in facts["running"]:
        try:
            metadata = json.loads(attempt["metadata_json"] or "{}")
        except ValueError:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        outcome = attempt_process.terminate_attempt(metadata)
        if outcome.get("status") not in {"terminated", "not_found", "already_dead"}:
            raise WorkItemConflictError(
                "Worker could not be stopped: "
                + str(outcome.get("reason") or outcome.get("status"))
            )
        metadata["termination"] = outcome
        metadata["process_stopped_at"] = now
        conn.execute(
            "UPDATE attempts SET metadata_json=? WHERE attempt_id=?",
            (json.dumps(metadata), attempt["attempt_id"]),
        )
    # This is an explicit operator attestation, never an automatic inference
    # that an unrecorded process died. The task locks prevent concurrent spawn.
    for run_id in abandon_review_run_ids:
        unknown[run_id]["store"].update_run(
            conn,
            run_id,
            status="abandoned",
            slot_reserved=0,
            error="operator confirmed untracked model processes were stopped",
        )
    from robert_agent.execution.rounds import terminate_children

    for (task_id,) in task_ids:
        if terminate_children(conn, task_id) == "attention":
            raise WorkItemConflictError(
                "Review worker identity could not be verified. Inspect and stop residual processes, then explicitly confirm abandonment of unknown launches when canceling."
            )
    from robert_agent.adapters.sqlite import execution_control as execution_store

    subjects = {("task", task_id) for (task_id,) in task_ids}
    for (task_id,) in task_ids:
        task = conn.execute(
            "SELECT task_kind, metadata_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if task and task[0] in {"ci_remediation", "merge_conflict_remediation"}:
            try:
                metadata = json.loads(task[1]) if task[1] else {}
            except (TypeError, ValueError):
                metadata = {}
            metadata = metadata or {}
            if metadata.get("remediation_episode_id"):
                subjects.add(
                    ("remediation_episode", metadata["remediation_episode_id"])
                )
    for subject_type, subject_id in sorted(subjects):
        binding = execution_store.get_binding(conn, subject_type, subject_id)
        if not binding or binding["binding_status"] == "terminal":
            continue
        execution_store.close_binding(conn, subject_type, subject_id, now)
        if subject_type == "remediation_episode":
            conn.execute(
                "UPDATE pr_remediation_episodes SET status='canceled', updated_at=? WHERE episode_id=? AND status NOT IN ('resolved','exhausted','superseded','canceled')",
                (now, subject_id),
            )
    placeholders = ",".join("?" for _ in facts["streams"]) or "NULL"
    params = [now] + facts["streams"]
    conn.execute(
        f"UPDATE tasks SET lifecycle='canceled', updated_at=? WHERE workstream_id IN ({placeholders}) AND lifecycle NOT IN ('completed','ignored','canceled')",
        params,
    )
    conn.execute(
        f"UPDATE attempts SET status='canceled', finished_at=? WHERE task_id IN (SELECT task_id FROM tasks WHERE workstream_id IN ({placeholders})) AND status IN ('prepared','running','stale')",
        params,
    )
    conn.execute(
        f"UPDATE workstreams SET lifecycle='canceled', active_task_id=NULL, updated_at=? WHERE workstream_id IN ({placeholders})",
        params,
    )
    conn.execute(
        f"UPDATE review_sessions SET status='canceled',error='operator canceled work item',updated_at=? WHERE workstream_id IN ({placeholders}) AND status NOT IN ('published','superseded','canceled')",
        params,
    )
    conn.execute(
        f"UPDATE vote_sessions SET status='canceled',error='operator canceled work item',updated_at=? WHERE workstream_id IN ({placeholders}) AND status NOT IN ('published','superseded','canceled')",
        params,
    )
    conn.execute(
        f"UPDATE github_actions SET publish_status='skipped' WHERE task_id IN (SELECT task_id FROM tasks WHERE workstream_id IN ({placeholders})) AND publish_status='not_published'",
        facts["streams"],
    )
    conn.execute(
        "UPDATE wakeups SET status='canceled', updated_at=? WHERE work_item_id=? AND status='pending'",
        (now, item["work_item_id"]),
    )
