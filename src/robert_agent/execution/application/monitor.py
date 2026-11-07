"""Observe worker attempts and apply bounded recovery decisions."""

from robert_agent.persistence.queries import attempts as db_read_attempts

from robert_agent.persistence import transactions as db_transactions
from robert_agent.persistence.dialects import sqlite as db_sqlite

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import worker_phases as db_read_worker_phases
from robert_agent.persistence.queries import worker_results as db_read_worker_results
from robert_agent.persistence.stores import attempts as db_write_attempts
from robert_agent.persistence.stores import execution_subjects as db_write_execution_subjects
from robert_agent.persistence.stores import tasks as db_write_tasks
from robert_agent.persistence.stores import workstreams as db_write_workstreams
from contextlib import contextmanager
from pathlib import Path
from robert_agent.adapters.processes import attempt as attempt_process
from robert_agent.execution.application import evidence
from robert_agent.work.application import commands as work_items
from robert_agent.work.application import lifecycle as work_application_lifecycle
from robert_agent.execution import inspection as supervise
from robert_agent.execution import notifications as execution_notifications
from robert_agent.execution import operations as execution_operations
from robert_agent.execution import ownership as execution_ownership
from robert_agent.execution import retry as retry_policy
from robert_agent.execution import spool as result_spool
from robert_agent.foundation import values
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.execution import rounds as native_rounds
import json
import subprocess


MAX_TRANSPORT_RESUMES = 5


def _supervision_observation_current(conn, attempt_id, task_id, snapshot, received_result, run_now):
    """Lock before validating an observation made outside the writer transaction."""
    if not conn.in_transaction:
        db_transactions.begin(conn, immediate=True)
    fresh = db_records.first(db_read_attempts.find_for_supervision_observation_current(conn, attempt_id, snapshot[0]))
    result = db_records.first(db_read_worker_results.find_result_id_by_attempt_id(conn, attempt_id))
    return bool(
        fresh is not None
        and tuple(fresh) == snapshot
        and (tuple(result) if result else None) == received_result
        and execution_ownership.claim_task(conn, task_id, run_now)
    )


@contextmanager
def _result_transition(conn, attempt_id, task_id, snapshot, received_result, run_now):
    """Serialize a destructive transition with durable submission and replay."""
    database = next((row[2] for row in db_sqlite.database_list(conn) if row[1] == "main"), "")
    conn.commit()
    if not database:
        yield _supervision_observation_current(conn, attempt_id, task_id, snapshot, received_result, run_now)
        return
    try:
        with result_spool.locked_record(database, attempt_id, blocking=False) as path:
            if path.exists():
                yield False
                return
            current = _supervision_observation_current(conn, attempt_id, task_id, snapshot, received_result, run_now)
            try:
                yield current
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()  # Never unlock while the terminal decision is uncommitted.
    except BlockingIOError:
        yield False


def _defer_attempt_inspection(conn, attempt_id, task_id, snapshot, received_result, run_now, exc):
    with _result_transition(conn, attempt_id, task_id, snapshot, received_result, run_now) as current:
        if not current:
            return
        metadata = values.json_object(snapshot[2])
        previous = metadata.get("inspection_retry", {})
        deferred = {
            **retry_policy.failure_state(previous, "Git inspection timed out", run_now),
            "status": "inspection_timeout",
            "timeout_seconds": exc.timeout,
        }
        db_write_attempts.update_for_defer_attempt_inspection(
            conn, json.dumps(deferred), json.dumps(deferred), attempt_id
        )
        if not deferred["retryable"]:
            # Process exit was proven before recovery inspection. Native children
            # still require their own exit proof before releasing the parent.
            conn.commit()
            stopped = native_rounds.terminate_children(conn, task_id) != "attention"
            stream = db_records.first(db_read_tasks.find_workstream_id_by_task_id(conn, task_id))[0]
            if stopped:
                db_write_attempts.update_for_defer_attempt_inspection_2(
                    conn, run_now, json.dumps(deferred), run_now, attempt_id
                )
                work_application_lifecycle.finalize_failed_task(
                    conn, task_id, stream, run_now, {"status": "inspection_retry_exhausted"}
                )
            else:
                execution_ownership.mark_attention(conn, task_id, run_now)
            item = work_items.resolve_work_item_for_workstream(conn, stream)
            if item:
                work_items.record_system_event(
                    conn,
                    item["work_item_id"],
                    event_type="execution_failed",
                    idempotency_key="inspection:" + attempt_id,
                    body="Git inspection retries exhausted. Inspect saved work and retry.",
                    metadata={"task_id": task_id},
                    now=run_now,
                )
            execution_notifications.insert(
                conn, "inspection_retry_exhausted", "recorded", run_now, {"task_id": task_id, **deferred}
            )


def supervise_running_attempts(
    conn,
    repo_id,
    config_result,
    run_now,
    data_dir=None,
    db_path=None,
    repo=None,
    *,
    resume_attempt,
    allow_recovery=True,
    task_id=None,
):
    rows = db_read_attempts.find_for_supervise_running_attempts(conn, repo_id, task_id, task_id)
    supervised = 0
    now_dt = values.parse_time(run_now)
    startup_grace_seconds = config_result.get("worker_startup_grace_seconds", 300)
    for (
        attempt_id,
        task_id,
        workstream_id,
        heartbeat_at,
        started_at,
        attempt_status,
        metadata_json,
    ) in rows:
        snapshot = (attempt_status, heartbeat_at, metadata_json)
        metadata = values.json_object(metadata_json)
        received_result = db_records.first(db_read_worker_results.find_result_id_by_attempt_id(conn, attempt_id))
        received_result = tuple(received_result) if received_result else None
        rejection = metadata.get("result_validation")
        if (not heartbeat_at or not started_at) and not rejection and not received_result:
            continue
        if rejection or received_result:
            started_at = started_at or metadata.get("worker_started_at") or run_now
            heartbeat_at = heartbeat_at or run_now
        if not execution_ownership.claim_task(conn, task_id, run_now):
            continue
        metadata = values.json_object(metadata_json)
        worker_launch = (metadata.get("dispatch") or {}).get("worker_launch") or {}
        worker_timeout_seconds = worker_launch.get("timeout_seconds")
        try:
            worker_timeout_seconds = int(worker_timeout_seconds)
        except (TypeError, ValueError):
            worker_timeout_seconds = None
        if worker_timeout_seconds is not None and worker_timeout_seconds < 1:
            worker_timeout_seconds = None
        if rejection:
            status = {**rejection, "ok": False, "terminate": True}
        else:
            status = supervise.classify_attempt(
                heartbeat_at=heartbeat_at,
                started_at=started_at,
                now=now_dt,
                heartbeat_stale_seconds=config_result.get("heartbeat_stale_seconds", 1200),
                global_timeout_seconds=config_result.get("hard_timeout_seconds", 2700),
                hard_timeout_seconds=worker_timeout_seconds,
            )
        process_status = {"status": "not_checked"}
        if attempt_status in {"running", "stale", "completed"} and status["status"] in {
            "running",
            "stale",
            "failed_timeout",
        }:
            conn.commit()  # Process observation may wait; never retain the domain writer.
            process_status = attempt_process.dispatch_status(metadata)
            if not _supervision_observation_current(conn, attempt_id, task_id, snapshot, received_result, run_now):
                continue
        if received_result and process_status["status"] != "unknown":
            if process_status["status"] == "dead" or status.get("terminate"):
                if process_status["status"] == "dead":
                    conn.commit()
                    outcome = {
                        "status": "attention"
                        if native_rounds.terminate_children(conn, task_id) == "attention"
                        else "already_dead"
                    }
                else:
                    outcome = terminate_owned_attempt(conn, task_id, metadata)
                if not _supervision_observation_current(conn, attempt_id, task_id, snapshot, received_result, run_now):
                    continue
                if outcome["status"] in {"terminated", "not_found", "already_dead"}:
                    fresh = db_records.first(db_read_attempts.find_status_by_attempt_id(conn, attempt_id))
                    if (
                        fresh
                        and fresh[0] in {"running", "stale", "completed"}
                        and execution_ownership.claim_task(conn, task_id, run_now)
                    ):
                        db_write_attempts.update_for_supervise_running_attempts_4(conn, run_now, run_now, attempt_id)
                        supervised += 1
                else:
                    execution_ownership.mark_attention(conn, task_id, run_now)
                continue
            if process_status["status"] == "alive_same":
                continue  # A received result is not an exited process.
        if process_status["status"] == "unknown":
            with _result_transition(conn, attempt_id, task_id, snapshot, received_result, run_now) as current:
                if not current:
                    continue
                supervised += 1
                attention = {
                    "ok": False,
                    "status": "worker_identity_attention",
                    "process": process_status,
                    "reason": "recorded Worker identity could not be verified",
                }
                metadata["supervise"] = attention
                execution_ownership.mark_attention(conn, task_id, run_now)
                db_write_attempts.set_metadata_json_by_attempt_id(
                    conn, json.dumps(metadata, ensure_ascii=False, sort_keys=True), attempt_id
                )
                execution_notifications.insert(
                    conn,
                    "worker_identity_attention",
                    "recorded",
                    run_now,
                    {
                        "task_id": task_id,
                        "attempt_id": attempt_id,
                        "workstream_id": workstream_id,
                    },
                )
                continue
        if process_status["status"] == "dead" and allow_recovery:
            if not evidence.inspection_due(metadata.get("inspection_retry", {}), run_now):
                continue
            worktree_row = db_records.first(db_read_attempts.find_worktree_path_by_attempt_id(conn, attempt_id))
            conn.commit()
            try:
                recovery = evidence.inspect_attempt_recovery_with_metadata(
                    conn, attempt_id, task_id, metadata, worktree_row[0] if worktree_row else None
                )
            except ControlDeadlineExceeded:
                raise
            except subprocess.TimeoutExpired as exc:
                _defer_attempt_inspection(conn, attempt_id, task_id, snapshot, received_result, run_now, exc)
                continue
            if not _supervision_observation_current(conn, attempt_id, task_id, snapshot, received_result, run_now):
                continue
            if recovery["status"] == "orphaned_command_running" and status["status"] != "failed_timeout":
                supervised += 1
                metadata["supervise"] = {
                    **process_status,
                    "ok": True,
                    "status": "orphaned_command_running",
                    "recovery": recovery,
                }
                db_write_attempts.set_metadata_json_by_attempt_id(
                    conn, json.dumps(metadata, ensure_ascii=False, sort_keys=True), attempt_id
                )
                continue
            transport_failure_count = int((metadata.get("resume") or {}).get("transport_failure_count", 0))
            transport_exhausted = bool(
                recovery.get("transport_failure") and transport_failure_count >= MAX_TRANSPORT_RESUMES
            )
            if (
                recovery["status"] in {"recoverable_progress", "transport_interrupted", "rate_limited"}
                and not transport_exhausted
                and data_dir is not None
                and db_path is not None
                and repo is not None
            ):
                with _result_transition(conn, attempt_id, task_id, snapshot, received_result, run_now) as current:
                    if not current:
                        continue
                    status = {
                        **process_status,
                        "ok": False,
                        "status": "needs_resume",
                        "original_status": (
                            "failed_timeout" if status["status"] == "failed_timeout" else "failed_worker_process_exited"
                        ),
                        "terminate": False,
                        "recovery": recovery,
                        "error_class": recovery.get("error_class"),
                    }
                    metadata["supervise"] = status
                    recovery_subject_type, recovery_subject_id = execution_ownership.recovery_subject(conn, task_id)
                    recovery_decision = execution_operations.classify_recovery(
                        conn,
                        recovery_subject_type,
                        recovery_subject_id,
                        attempt_id,
                        {"identity_status": "dead", **recovery},
                        now=run_now,
                    )
                    if recovery_decision == "attention":
                        supervised += 1
                        reason = recovery.get("reason_code") or "recovery_budget_exhausted"
                        failure = {"status": reason}
                        if recovery.get("rate_limit_failure"):
                            failure.update(error_class="rate_limited", recovery=recovery)
                        db_write_attempts.update_for_supervise_running_attempts_5(
                            conn, run_now, json.dumps(failure), attempt_id
                        )
                        db_write_tasks.update_for_supervise_running_attempts(conn, run_now, task_id)
                        db_write_workstreams.update_for_supervise_running_attempts(conn, run_now, workstream_id)
                        metadata["supervise"] = {
                            **status,
                            "status": "worker_identity_attention",
                            "recovery_decision": recovery_decision,
                            "reason": recovery.get("reason_code")
                            or "Automatic recovery quota exhausted; inspect and cancel this execution before retrying.",
                        }
                        db_write_attempts.set_metadata_json_by_attempt_id(
                            conn, json.dumps(metadata, ensure_ascii=False, sort_keys=True), attempt_id
                        )
                        execution_notifications.insert(
                            conn,
                            "worker_identity_attention",
                            "recorded",
                            run_now,
                            {
                                "task_id": task_id,
                                "attempt_id": attempt_id,
                                "workstream_id": workstream_id,
                            },
                        )
                        continue
                    resume = resume_attempt(
                        conn,
                        Path(data_dir),
                        Path(db_path),
                        repo,
                        repo_id,
                        task_id,
                        workstream_id,
                        attempt_id,
                        run_now,
                        recovery,
                    )
                    if resume:
                        supervised += 1
                        metadata["resume"] = resume
                        db_write_attempts.update_for_supervise_running_attempts_2(
                            conn,
                            run_now,
                            json.dumps(status, sort_keys=True),
                            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                            attempt_id,
                        )
                        execution_notifications.insert(
                            conn,
                            "worker_resume_prepared",
                            "recorded",
                            run_now,
                            {
                                "task_id": task_id,
                                "attempt_id": attempt_id,
                                "resume_attempt_id": resume["attempt_id"],
                                "workstream_id": workstream_id,
                                "recovery_artifact_path": resume["recovery_artifact_path"],
                            },
                        )
                        continue
                    _record_recovery_creation_attention(
                        conn,
                        attempt_id=attempt_id,
                        task_id=task_id,
                        workstream_id=workstream_id,
                        metadata=metadata,
                        status=status,
                        run_now=run_now,
                    )
                    supervised += 1
                    continue
            if status["status"] == "failed_timeout" and not transport_exhausted:
                status = {**status, "process": process_status, "recovery": recovery}
            else:
                status = {
                    **process_status,
                    "ok": False,
                    "status": (
                        "failed_transport_retries_exhausted" if transport_exhausted else "failed_worker_process_exited"
                    ),
                    "terminate": False,
                }
            category = recovery.get("error_class")
            if recovery.get("rate_limit_failure"):
                status = {
                    **status,
                    "status": "rate_limited",
                    "error_class": "rate_limited",
                    "reason_code": "rate_limited",
                    "recovery": recovery,
                    "safe_error": recovery["rate_limit_failure"]["message"],
                }
            if category and category not in {"transient_failure", "rate_limited"}:
                status = {
                    **status,
                    "status": category,
                    "reason_code": category,
                    "safe_error": "Worker stopped: " + category,
                    "required_action": "Repair the reported cause, then retry the task.",
                }
        elif process_status["status"] == "dead":
            status = {
                "ok": False,
                "status": "failed_worker_process_exited",
                "terminate": False,
                "reason_code": "control_paused",
                "required_action": "Repair the paused controller, then explicitly retry this task.",
            }
        if process_status["status"] == "dead" and metadata.get("submission_validation"):
            status = {**metadata["submission_validation"], "terminate": False}
        runtime_seconds = (now_dt - values.parse_time(started_at)).total_seconds()
        has_worker_phase = db_records.first(db_read_worker_phases.find_for_supervise_running_attempts(conn, attempt_id))
        if not has_worker_phase and runtime_seconds >= startup_grace_seconds:
            conn.commit()
            has_worker_phase = bool(evidence.attempt_command_records(conn, attempt_id, task_id, persist=False))
            if not _supervision_observation_current(conn, attempt_id, task_id, snapshot, received_result, run_now):
                continue
        if (
            status["status"] in {"running", "stale"}
            and runtime_seconds >= startup_grace_seconds
            and not has_worker_phase
        ):
            status = {
                "ok": False,
                "status": "failed_worker_startup",
                "terminate": True,
                "startup_grace_seconds": startup_grace_seconds,
                "runtime_seconds": runtime_seconds,
            }
        metadata["supervise"] = status
        if status["status"] == "running":
            if attempt_status == "stale":
                supervised += 1
                db_write_attempts.update_for_supervise_running_attempts_3(
                    conn, json.dumps(metadata, ensure_ascii=False, sort_keys=True), attempt_id
                )
            continue
        supervised += 1
        if status["status"] == "stale":
            db_write_attempts.update_for_supervise_running_attempts(
                conn, json.dumps(metadata, ensure_ascii=False, sort_keys=True), attempt_id
            )
            if attempt_status != "stale":
                execution_notifications.insert(
                    conn,
                    "worker_stale",
                    "recorded",
                    run_now,
                    {"task_id": task_id, "attempt_id": attempt_id, "workstream_id": workstream_id},
                )
            continue
        previous_termination = metadata.get("termination")
        with _result_transition(conn, attempt_id, task_id, snapshot, received_result, run_now) as current:
            if not current:
                continue
            metadata["termination"] = terminate_owned_attempt(conn, task_id, metadata)
            if not _supervision_observation_current(conn, attempt_id, task_id, snapshot, received_result, run_now):
                continue
            if metadata["termination"]["status"] not in {"terminated", "not_found", "already_dead"}:
                if previous_termination != metadata["termination"]:
                    execution_notifications.insert(
                        conn,
                        "worker_cleanup_pending",
                        "recorded",
                        run_now,
                        {"task_id": task_id, "attempt_id": attempt_id, "termination": metadata["termination"]},
                    )
                # Keep ownership and capacity until process cleanup is confirmed.
                db_write_attempts.update_for_supervise_running_attempts_3(
                    conn, json.dumps(metadata, ensure_ascii=False, sort_keys=True), attempt_id
                )
                continue
            db_write_attempts.update_for_supervise_running_attempts_2(
                conn,
                run_now,
                json.dumps(status, sort_keys=True),
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                attempt_id,
            )
            work_application_lifecycle.finalize_failed_task(conn, task_id, workstream_id, run_now, status)
            if not allow_recovery:
                execution_ownership.close_terminal_task(conn, task_id, run_now)
            execution_notifications.insert(
                conn,
                _supervision_failure_notification_type(status["status"]),
                "recorded",
                run_now,
                {
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "workstream_id": workstream_id,
                    **{
                        key: status[key]
                        for key in ("reason_code", "field_path", "findings", "safe_error", "required_action")
                        if key in status
                    },
                },
            )
    return supervised


def recover_failed_timeout_attempts(
    conn,
    repo_id,
    run_now,
    data_dir=None,
    db_path=None,
    repo=None,
    *,
    resume_attempt,
):
    if data_dir is None or db_path is None or repo is None:
        return 0
    rows = db_read_attempts.find_for_recover_failed_timeout_attempts(conn, repo_id, run_now)
    recovered = 0
    for (
        attempt_id,
        task_id,
        workstream_id,
        worktree_path,
        failure_json,
        metadata_json,
        heartbeat_at,
    ) in rows:
        failure = values.json_object(failure_json)
        if failure.get("status") != "failed_timeout":
            continue
        if not execution_ownership.claim_task(conn, task_id, run_now):
            continue
        metadata = values.json_object(metadata_json)
        if not evidence.inspection_due(metadata.get("inspection_retry", {}), run_now):
            continue
        snapshot = ("failed", heartbeat_at, metadata_json)
        conn.commit()
        process_status = attempt_process.dispatch_status(metadata)
        if not _supervision_observation_current(conn, attempt_id, task_id, snapshot, None, run_now):
            continue
        if process_status["status"] == "unknown":
            execution_ownership.mark_attention(conn, task_id, run_now)
            metadata["supervise"] = {
                "ok": False,
                "status": "worker_identity_attention",
                "process": process_status,
            }
            db_write_attempts.set_metadata_json_by_attempt_id(
                conn, json.dumps(metadata, ensure_ascii=False, sort_keys=True), attempt_id
            )
            execution_notifications.insert(
                conn,
                "worker_identity_attention",
                "recorded",
                run_now,
                {
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "workstream_id": workstream_id,
                },
            )
            continue
        if process_status["status"] != "dead":
            continue
        conn.commit()
        try:
            recovery = evidence.inspect_attempt_recovery_with_metadata(
                conn, attempt_id, task_id, metadata, worktree_path
            )
        except ControlDeadlineExceeded:
            raise
        except subprocess.TimeoutExpired as exc:
            _defer_attempt_inspection(conn, attempt_id, task_id, snapshot, None, run_now, exc)
            continue
        if not _supervision_observation_current(conn, attempt_id, task_id, snapshot, None, run_now):
            continue
        if recovery["status"] not in {"recoverable_progress", "transport_interrupted"}:
            _record_recovery_creation_attention(
                conn,
                attempt_id=attempt_id,
                task_id=task_id,
                workstream_id=workstream_id,
                metadata=metadata,
                status={"original_status": "failed_timeout", "recovery": recovery},
                run_now=run_now,
                reason="timeout_recovery_unavailable",
            )
            continue
        status = {
            **process_status,
            "ok": False,
            "status": "needs_resume",
            "original_status": "failed_timeout",
            "terminate": False,
            "recovery": recovery,
        }
        metadata["supervise"] = status
        recovery_subject_type, recovery_subject_id = execution_ownership.recovery_subject(conn, task_id)
        recovery_decision = execution_operations.classify_recovery(
            conn,
            recovery_subject_type,
            recovery_subject_id,
            attempt_id,
            {"identity_status": "dead", **recovery},
            now=run_now,
        )
        if recovery_decision == "attention":
            metadata["supervise"] = {
                **status,
                "status": "worker_identity_attention",
                "recovery_decision": recovery_decision,
                "reason": recovery.get("reason_code")
                or "Automatic recovery quota exhausted; inspect and cancel this execution before retrying.",
            }
            db_write_attempts.set_metadata_json_by_attempt_id(
                conn, json.dumps(metadata, ensure_ascii=False, sort_keys=True), attempt_id
            )
            execution_notifications.insert(
                conn,
                "worker_identity_attention",
                "recorded",
                run_now,
                {
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "workstream_id": workstream_id,
                },
            )
            continue
        resume = resume_attempt(
            conn,
            Path(data_dir),
            Path(db_path),
            repo,
            repo_id,
            task_id,
            workstream_id,
            attempt_id,
            run_now,
            recovery,
        )
        if not resume:
            _record_recovery_creation_attention(
                conn,
                attempt_id=attempt_id,
                task_id=task_id,
                workstream_id=workstream_id,
                metadata=metadata,
                status=status,
                run_now=run_now,
            )
            continue
        recovered += 1
        metadata["resume"] = resume
        db_write_attempts.update_for_recover_failed_timeout_attempts(
            conn,
            run_now,
            json.dumps(status, sort_keys=True),
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            attempt_id,
        )
        execution_notifications.insert(
            conn,
            "worker_resume_prepared",
            "recorded",
            run_now,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "resume_attempt_id": resume["attempt_id"],
                "workstream_id": workstream_id,
                "recovery_artifact_path": resume["recovery_artifact_path"],
            },
        )
    return recovered


def _supervision_failure_notification_type(status):
    if status in {"authentication_required", "permission_required", "configuration_required", "branch_conflict"}:
        return "worker_" + status
    if status == "failed_validation":
        return "worker_result_validation_failed"
    if status == "failed_worker_startup":
        return "worker_startup_failed"
    if status == "failed_worker_process_exited":
        return "worker_process_exited"
    if status == "failed_transport_retries_exhausted":
        return "worker_transport_retries_exhausted"
    return "worker_timeout"


def terminate_owned_attempt(conn, task_id, metadata):
    """Stop the coordinator and its registered model groups outside the writer."""
    conn.commit()
    outcome = attempt_process.terminate_attempt(metadata)
    if outcome.get("status") in {"terminated", "not_found", "already_dead"}:
        if native_rounds.terminate_children(conn, task_id) == "attention":
            return {"status": "attention", "reason": "native_model_cleanup_unconfirmed"}
    return outcome


def _record_recovery_creation_attention(
    conn,
    *,
    attempt_id,
    task_id,
    workstream_id,
    metadata,
    status,
    run_now,
    reason="recovery_attempt_creation_failed",
):
    """Stop automatic recovery without changing its already-recorded quota."""

    metadata["supervise"] = {
        **status,
        "status": "worker_identity_attention",
        "recovery_decision": "attention",
        "reason": reason,
    }
    execution_ownership.mark_attention(conn, task_id, run_now)
    subject_type, subject_id = execution_ownership.recovery_subject(conn, task_id)
    db_write_execution_subjects.record_recovery_creation_attention(
        conn, reason, "Inspect saved work and process evidence, then retry or cancel.", subject_type, subject_id
    )
    db_write_attempts.set_metadata_json_by_attempt_id(
        conn, json.dumps(metadata, ensure_ascii=False, sort_keys=True), attempt_id
    )
    execution_notifications.insert(
        conn,
        "worker_identity_attention",
        "recorded",
        run_now,
        {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "workstream_id": workstream_id,
            "reason": reason,
        },
    )
