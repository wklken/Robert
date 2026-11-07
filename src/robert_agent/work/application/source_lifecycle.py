"""Reconcile terminal GitHub source state with owned work."""

from robert_agent.foundation.contracts.execution import AttemptStatus, BindingStatus
from robert_agent.foundation.contracts.work import REMEDIATION_TASK_KINDS, WorkstreamLifecycle

from robert_agent.persistence.queries import attempts as db_read_attempts

from robert_agent.persistence import transactions as db_transactions
from robert_agent.persistence.dialects import sqlite as db_sqlite

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_sources as db_read_github_sources
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import workstreams as db_read_workstreams
from robert_agent.persistence.stores import attempts as db_write_attempts
from robert_agent.persistence.stores import audit_events as db_write_audit_events
from robert_agent.persistence.stores import github_events as db_write_github_events
from robert_agent.persistence.stores import github_sources as db_write_github_sources
from robert_agent.persistence.stores import tasks as db_write_tasks
from robert_agent.persistence.stores import work_items as db_write_work_items
from robert_agent.persistence.stores import workstreams as db_write_workstreams
from robert_agent.delivery.application import outcomes as publication_outcomes
from robert_agent.adapters.github import sources as github_sources
from robert_agent.persistence import transactions as transactions
from robert_agent.execution.application import monitor
from robert_agent.work.application import commands as work_items
from robert_agent.work import control_queries
from robert_agent.execution import notifications as execution_notifications
from robert_agent.execution import operations as execution_operations
from robert_agent.execution import ownership as execution_ownership
from robert_agent.persistence import execution_control as execution_store
from robert_agent.foundation import values
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.work import queries as work_queries
from robert_agent.workflows import pr_remediation as remediation
import json


def _remote_terminal_state(source_type, remote_state):
    if (remote_state.get("state") or "").lower() != "closed":
        return None
    if source_type == "pull_request":
        if remote_state.get("merged"):
            return WorkstreamLifecycle.COMPLETED.value, "remote_pr_merged"
        return WorkstreamLifecycle.CANCELED.value, "remote_pr_closed"
    if remote_state.get("state_reason") == "completed":
        return WorkstreamLifecycle.COMPLETED.value, "remote_source_closed"
    return WorkstreamLifecycle.CANCELED.value, "remote_source_closed"


def record_remote_terminal_state(
    conn,
    repo_id,
    workstream_id,
    source_id,
    source_key,
    source_type,
    number,
    task_id,
    remote_state,
    workstream_lifecycle,
    audit_type,
    run_now,
):
    from robert_agent.delivery.lock import task_publication_lock

    database = next((row[2] for row in db_sqlite.database_list(conn) if row[1] == "main"), "")
    args = (
        conn,
        repo_id,
        workstream_id,
        source_id,
        source_key,
        source_type,
        number,
        task_id,
        remote_state,
        workstream_lifecycle,
        audit_type,
        run_now,
    )
    if not task_id or not database:
        return _record_remote_terminal_state_locked(*args)
    conn.commit()
    with task_publication_lock(database, task_id) as locked:
        if not locked:
            return False  # Reobserve after the in-flight publisher/launcher commits.
        try:
            result = _record_remote_terminal_state_locked(*args)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise


def _record_remote_terminal_state_locked(
    conn,
    repo_id,
    workstream_id,
    source_id,
    source_key,
    source_type,
    number,
    task_id,
    remote_state,
    workstream_lifecycle,
    audit_type,
    run_now,
):
    subject_type, subject_id = "task", task_id
    if task_id:
        task_row = db_records.first(db_read_tasks.find_task_kind_and_metadata_json_by_task_id(conn, task_id))
        task_metadata = values.json_object(task_row[1] if task_row else None)
        episode_id = task_metadata.get("remediation_episode_id") if task_row else None
        if task_row and task_row[0] in REMEDIATION_TASK_KINDS and episode_id:
            subject_type, subject_id = "remediation_episode", str(episode_id)
    binding = execution_store.get_binding(conn, subject_type, subject_id) if subject_id else None
    workflow_fact = None
    if binding and binding["binding_status"] == BindingStatus.ACTIVE:
        marker = (
            remote_state.get("merged_at")
            or remote_state.get("closed_at")
            or remote_state.get("updated_at")
            or remote_state.get("state")
            or audit_type
        )
        workflow_fact = {
            "fact_id": f"remote-terminal:{source_key}:{marker}",
            "subject_type": subject_type,
            "subject_id": subject_id,
            "repo": source_key.split(":", 1)[-1].split("#", 1)[0].split("!", 1)[0],
            "source_type": source_type,
            "source_key": source_key,
            "number": number,
            "event_type": "source_state",
            "authorization_status": "authorized_system_trigger",
            "source_state": remote_state.get("state"),
            "merged": bool(remote_state.get("merged") or remote_state.get("merged_at")),
        }
        execution_operations.apply_classified_fact(
            conn,
            subject_type,
            subject_id,
            workflow_fact,
            now=run_now,
        )
    if task_id and not binding and not execution_ownership.reconcile_task_allowed(conn, task_id, run_now):
        return False
    source_metadata_row = db_records.first(
        db_read_github_sources.find_metadata_json_and_state_by_source_id(conn, source_id)
    )
    source_metadata = values.json_object(source_metadata_row[0] if source_metadata_row else None)
    attention = binding and binding["binding_status"] == BindingStatus.ATTENTION
    if (
        attention
        and source_metadata_row
        and source_metadata_row[1] == remote_state.get("state")
        and source_metadata.get("remote_state") == remote_state
    ):
        return False
    source_metadata["remote_state"] = remote_state
    db_write_github_sources.record_remote_terminal_state_locked(
        conn,
        remote_state.get("state") or "closed",
        remote_state.get("html_url"),
        remote_state.get("title") or "",
        remote_state.get("updated_at"),
        json.dumps(source_metadata, ensure_ascii=False, sort_keys=True),
        source_id,
    )
    if attention:
        return False
    attempts = db_read_attempts.record_remote_terminal_state_locked(conn, task_id)
    attempt_metadata = {}
    cleanup_pending = False
    for attempt_id, attempt_status, metadata_json in attempts:
        metadata = values.json_object(metadata_json)
        if attempt_status in {AttemptStatus.RUNNING, AttemptStatus.STALE, AttemptStatus.COMPLETED}:
            previous_termination = metadata.get("termination")
            metadata["termination"] = monitor.terminate_owned_attempt(conn, task_id, metadata)
            if metadata["termination"]["status"] not in {"terminated", "not_found", "already_dead"}:
                cleanup_pending = True
                if previous_termination != metadata["termination"]:
                    execution_notifications.insert(
                        conn,
                        "worker_cleanup_pending",
                        "recorded",
                        run_now,
                        {"task_id": task_id, "attempt_id": attempt_id, "termination": metadata["termination"]},
                    )
            db_write_attempts.update_for_supervise_running_attempts_3(
                conn, json.dumps(metadata, sort_keys=True), attempt_id
            )
        attempt_metadata[attempt_id] = metadata
    if cleanup_pending:
        return False
    remote_payload = {
        "status": audit_type,
        "source_key": source_key,
        "source_type": source_type,
        "number": number,
        "remote_state": remote_state,
    }
    db_write_tasks.record_remote_terminal_state_locked(
        conn,
        run_now,
        json.dumps({"remote_reconciliation": remote_payload}, ensure_ascii=False, sort_keys=True),
        task_id,
    )
    for attempt_id, metadata in attempt_metadata.items():
        metadata["remote_reconciliation"] = remote_payload
        db_write_attempts.record_remote_terminal_state_locked(
            conn, run_now, json.dumps(remote_payload, sort_keys=True), json.dumps(metadata, sort_keys=True), attempt_id
        )
    execution_ownership.close_terminal_task(conn, task_id, run_now)
    publication_outcomes.retire_unstarted_actions(conn, task_id, run_now)
    workstream_metadata_row = db_records.first(
        db_read_workstreams.find_metadata_json_by_workstream_id(conn, workstream_id)
    )
    workstream_metadata = values.json_object(workstream_metadata_row[0] if workstream_metadata_row else None)
    workstream_metadata["remote_reconciliation"] = remote_payload
    db_write_workstreams.record_remote_terminal_state_locked(
        conn,
        workstream_lifecycle,
        run_now,
        json.dumps(workstream_metadata, ensure_ascii=False, sort_keys=True),
        workstream_id,
    )
    db_write_audit_events.record_remote_terminal_state_locked(
        conn,
        values.new_id("audit"),
        repo_id,
        workstream_id,
        task_id,
        audit_type,
        run_now,
        json.dumps(remote_payload, ensure_ascii=False, sort_keys=True),
    )
    item = work_items.resolve_work_item_for_workstream(conn, workstream_id)
    if item and source_type == "issue" and item["origin_source_id"] == source_id:
        facts = control_queries.lifecycle_facts(conn, item)
        unfinished = db_records.first(
            db_read_tasks.record_remote_terminal_state_locked(conn, item["workstream_id"], item["workstream_id"])
        )
        if (
            not unfinished
            and not facts["running"]
            and not facts["actions"]
            and not facts["cleanup_pending"]
            and not facts["workflow_attention"]
        ):
            terminal_column = "completed_at" if workstream_lifecycle == WorkstreamLifecycle.COMPLETED else "canceled_at"
            decision = work_items._unresolved_event(conn, item["work_item_id"], work_items.DECISION_EVENTS)
            work_items.record_system_event(
                conn,
                item["work_item_id"],
                event_type="completed" if workstream_lifecycle == WorkstreamLifecycle.COMPLETED else "canceled",
                idempotency_key=f"issue-closed:{source_key}:{remote_state.get('closed_at') or run_now}",
                body="GitHub issue closed: " + (remote_state.get("state_reason") or "closed"),
                resolves_event_id=(decision["event_id"] if hasattr(decision, "keys") else decision[0])
                if decision
                else None,
                metadata={"source_key": source_key, "number": number},
                now=run_now,
            )
            db_write_work_items.mark_remote_terminal(
                conn, item["work_item_id"], terminal_column, remote_state.get("closed_at") or run_now, run_now
            )
    if item and source_type == "pull_request":
        if audit_type == "remote_pr_merged":
            work_items.record_system_event(
                conn,
                item["work_item_id"],
                event_type="pr_merged",
                idempotency_key=f"pr-merged:{source_key}:{remote_state.get('merged_at') or run_now}",
                metadata={"source_key": source_key, "number": number},
                now=run_now,
            )
            db_write_work_items.update_for_finalize_accepted_result(
                conn, remote_state.get("merged_at") or run_now, run_now, item["work_item_id"]
            )
            db_write_workstreams.record_remote_terminal_state_locked_2(conn, run_now, item["work_item_id"])
        elif audit_type == "remote_pr_closed":
            work_items.record_system_event(
                conn,
                item["work_item_id"],
                event_type="unmerged_pr_closed",
                idempotency_key=f"pr-closed-unmerged:{source_key}:{remote_state.get('closed_at') or run_now}",
                body="Pull request closed without merge.",
                metadata={"source_key": source_key, "number": number},
                now=run_now,
            )
    return True


def _current_state_terminal_reason(event):
    state = (event.get("state") or "").lower()
    merged = bool(event.get("merged") or event.get("merged_at"))
    if state != "closed" and not merged:
        return None
    remote_state = {
        "state": "closed" if merged else state,
        "state_reason": event.get("state_reason"),
        "title": event.get("title") or "",
        "html_url": event.get("url"),
        "updated_at": event.get("source_updated_at") or event.get("event_at"),
        "closed_at": event.get("closed_at"),
        "merged": merged,
        "merged_at": event.get("merged_at"),
    }
    terminal = _remote_terminal_state(event.get("source_type"), remote_state)
    if not terminal:
        return None
    _workstream_lifecycle, terminal_reason = terminal
    return terminal_reason, remote_state


def _source_has_active_unpublished_result(conn, source_id):
    row = db_records.first(
        db_read_workstreams.find_for_source_has_active_unpublished_result(conn, source_id, source_id)
    )
    return bool(row)


def _record_current_state_closed_source(
    conn,
    repo_id,
    source_id,
    event_id,
    event,
    terminal_reason,
    remote_state,
    run_now,
):
    source_metadata_row = db_records.first(db_read_github_sources.find_metadata_json_by_source_id(conn, source_id))
    source_metadata = values.json_object(source_metadata_row[0] if source_metadata_row else None)
    payload = {
        "status": "current_state_closed",
        "source_key": event["source_key"],
        "source_type": event["source_type"],
        "number": event["number"],
        "terminal_reason": terminal_reason,
        "skip_reason": "source_already_closed_before_dispatch",
        "remote_state": remote_state,
    }
    source_metadata["current_state_reconciliation"] = payload
    db_write_github_sources.record_current_state_closed_source(
        conn,
        remote_state.get("html_url"),
        remote_state.get("title") or "",
        remote_state.get("updated_at"),
        json.dumps(source_metadata, ensure_ascii=False, sort_keys=True),
        source_id,
    )
    db_write_github_events.record_current_state_closed_source(conn, event_id)
    db_write_audit_events.record_current_state_closed_source(
        conn, values.new_id("audit"), repo_id, run_now, json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )
    return payload


def record_current_state_closed_source_if_needed(conn, repo_id, source_id, event_id, event, run_now):
    terminal = _current_state_terminal_reason(event)
    if not terminal:
        return None
    if _source_has_active_unpublished_result(conn, source_id):
        return None
    terminal_reason, remote_state = terminal
    return _record_current_state_closed_source(
        conn,
        repo_id,
        source_id,
        event_id,
        event,
        terminal_reason,
        remote_state,
        run_now,
    )


def reconcile_remote_source_states(conn, repo_id, repo, runner, run_now):
    rows = work_queries.remote_source_rows(conn, repo_id)
    reconciled = 0
    for workstream_id, task_id, source_id, source_key, source_type, number in rows:
        task_id = task_id or work_queries.latest_task_id(conn, workstream_id)
        conn.commit()
        db_transactions.savepoint(conn, "reconcile_source")
        source_succeeded = False
        try:
            remote_state = github_sources.read_source_state(repo["full_name"], source_type, number, runner)
            if not remote_state:
                continue
            terminal = _remote_terminal_state(source_type, remote_state)
            if not terminal:
                source_succeeded = True
                continue
            workstream_lifecycle, audit_type = terminal
            recorded = record_remote_terminal_state(
                conn,
                repo_id,
                workstream_id,
                source_id,
                source_key,
                source_type,
                number,
                task_id,
                remote_state,
                workstream_lifecycle,
                audit_type,
                run_now,
            )
            if not recorded:
                source_succeeded = True
                continue
            remediation.cancel_workstream_episodes(
                conn,
                workstream_id=workstream_id,
                reason="remote_pr_terminal",
                now=run_now,
            )
            reconciled += 1
            source_succeeded = True
        except ControlDeadlineExceeded:
            transactions.finish_isolation(conn, "reconcile_source", rollback=True)
            raise
        except Exception as exc:
            transactions.finish_isolation(conn, "reconcile_source", rollback=True)
            execution_notifications.update_failure(
                conn,
                "source_reconcile_failed",
                source_key,
                run_now,
                {
                    "source_key": source_key,
                    "task_id": task_id,
                    "reason_code": "source_reconcile_failed",
                    "error_type": type(exc).__name__,
                    "required_action": "Inspect this source; other sources continue and this source will be retried.",
                },
            )
        finally:
            if source_succeeded:
                execution_notifications.update_failure(conn, "source_reconcile_failed", source_key, run_now)
            transactions.finish_isolation(conn, "reconcile_source")
    return reconciled
