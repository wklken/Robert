"""Task lifecycle transitions shared by execution and publication."""

from robert_agent.foundation.contracts.work import TaskKind

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_actions as db_read_github_actions
from robert_agent.persistence.queries import worker_results as db_read_worker_results
from robert_agent.persistence.stores import tasks as db_write_tasks
from robert_agent.persistence.stores import wakeups as db_write_wakeups
from robert_agent.persistence.stores import workstreams as db_write_workstreams
import json
from robert_agent.execution import rounds as native_rounds
from robert_agent.workflows import pr_remediation as remediation
from robert_agent.work.application import commands as work_items
from robert_agent.execution import ownership as execution_ownership


def _json_object(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def finalize_failed_task(conn, task_id, workstream_id, run_now, audit):
    if not execution_ownership.claim_task(conn, task_id, run_now):
        return False
    round_store, entry = native_rounds.owner(conn, task_id)
    if entry and native_rounds.terminate_children(conn, task_id) == "attention":
        round_store.update(conn, entry["round_id"], status="attention", error="model child identity unknown")
    db_write_tasks.update_for_supervise_running_attempts(conn, run_now, task_id)
    db_write_workstreams.update_for_finalize_failed_task(
        conn, run_now, json.dumps({"last_audit": audit}, ensure_ascii=False, sort_keys=True), workstream_id
    )
    remediation.record_task_failure(
        conn,
        task_id=task_id,
        reason=(audit.get("status") if isinstance(audit, dict) else "task_failed") or "task_failed",
        now=run_now,
    )
    if not (isinstance(audit, dict) and audit.get("status") == "failed_timeout"):
        execution_ownership.close_terminal_task(conn, task_id, run_now)
    return True


def mark_task_waiting_for_publish(conn, task_id, workstream_id, run_now):
    db_write_tasks.mark_task_waiting_for_publish(conn, run_now, task_id)
    db_write_workstreams.update_for_create_task(conn, task_id, run_now, workstream_id)


def mark_task_waiting_for_user(conn, task_id, workstream_id, run_now):
    db_write_tasks.mark_task_waiting_for_user(conn, run_now, task_id)
    db_write_workstreams.mark_task_waiting_for_user(conn, task_id, run_now, workstream_id)


def finalize_superseded_result(conn, result_id, now):
    row = db_records.first(db_read_worker_results.find_for_finalize_superseded_result(conn, result_id))
    if not row or row[2] != TaskKind.HUMAN_REQUEST.value or not execution_ownership.claim_task(conn, row[0], now):
        return False
    actions = db_read_github_actions.find_publish_status_and_metadata_json_by_result_id(conn, result_id)
    if not actions or any(status not in {"published", "skipped"} for status, _ in actions):
        return False
    if not any(
        status == "skipped" and _json_object(encoded).get("publish", {}).get("reason_code") == "superseded"
        for status, encoded in actions
    ):
        return False
    finalize_failed_task(
        conn,
        row[0],
        row[1],
        now,
        {"status": "publication_superseded", "safe_error": "The remote branch changed. Retry from the current head."},
    )
    item = work_items.resolve_work_item_for_workstream(conn, row[1])
    if item:
        work_items.record_system_event(
            conn,
            item["work_item_id"],
            event_type="execution_failed",
            idempotency_key="superseded:" + result_id,
            body="The remote branch changed. Retry from the current head.",
            metadata={"task_id": row[0], "result_id": result_id},
            now=now,
        )
    db_write_wakeups.update_for_finalize_superseded_result(conn, now, result_id)
    return True
