"""Shared lifecycle facts and safe operator recovery for a work item."""

from robert_agent.persistence.stores import review_sessions as db_write_review_sessions
from robert_agent.persistence.stores import tasks as db_write_tasks
from robert_agent.persistence.stores import vote_sessions as db_write_vote_sessions
from robert_agent.persistence.stores import workstreams as db_write_workstreams

from robert_agent.persistence.stores import github_actions as db_write_github_actions

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import leases as db_read_leases
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.stores import attempts as db_write_attempts
from robert_agent.persistence.stores import pr_remediation_episodes as db_write_pr_remediation_episodes
from robert_agent.persistence.stores import wakeups as db_write_wakeups
from robert_agent.adapters.processes import attempt as attempt_process
import json

from robert_agent.delivery.lock import task_publication_lock


def require_idle_repository(conn, repo_id, now):
    from robert_agent.work.errors import WorkItemConflictError

    busy = db_records.first(db_read_leases.find_for_require_idle_repository(conn, repo_id, now))
    if busy:
        raise WorkItemConflictError("The repository is processing a cycle. Refresh and retry after it finishes.")


def cancel_execution(conn, item, facts, now, db_path, locks, *, abandon_review_run_ids=()):
    from robert_agent.work.errors import WorkItemConflictError

    # The write transaction prevents a new engine lease. Publication has its
    # own task lock, held until this command commits; neither lock waits on I/O.
    require_idle_repository(conn, item["repo_id"], now)
    task_ids = db_read_tasks.cancel_execution_filtered(conn, facts["streams"])
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
                "Worker could not be stopped: " + str(outcome.get("reason") or outcome.get("status"))
            )
        metadata["termination"] = outcome
        metadata["process_stopped_at"] = now
        db_write_attempts.set_metadata_json_by_attempt_id(conn, json.dumps(metadata), attempt["attempt_id"])
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
    from robert_agent.persistence import execution_control as execution_store

    subjects = {("task", task_id) for (task_id,) in task_ids}
    for (task_id,) in task_ids:
        task = db_records.first(db_read_tasks.find_task_kind_and_metadata_json_by_task_id(conn, task_id))
        if task and task[0] in {"ci_remediation", "merge_conflict_remediation"}:
            try:
                metadata = json.loads(task[1]) if task[1] else {}
            except (TypeError, ValueError):
                metadata = {}
            metadata = metadata or {}
            if metadata.get("remediation_episode_id"):
                subjects.add(("remediation_episode", metadata["remediation_episode_id"]))
    for subject_type, subject_id in sorted(subjects):
        binding = execution_store.get_binding(conn, subject_type, subject_id)
        if not binding or binding["binding_status"] == "terminal":
            continue
        execution_store.close_binding(conn, subject_type, subject_id, now)
        if subject_type == "remediation_episode":
            db_write_pr_remediation_episodes.update_for_cancel_execution(conn, now, subject_id)
    db_write_tasks.cancel_execution_filtered(conn, facts["streams"], now)
    db_write_attempts.cancel_execution_filtered(conn, facts["streams"], now)
    db_write_workstreams.cancel_execution_filtered(conn, facts["streams"], now)
    db_write_review_sessions.cancel_execution_filtered(conn, facts["streams"], now)
    db_write_vote_sessions.cancel_execution_filtered(conn, facts["streams"], now)
    db_write_github_actions.cancel_execution_filtered(conn, facts["streams"])
    db_write_wakeups.update_for_cancel_execution(conn, now, item["work_item_id"])
