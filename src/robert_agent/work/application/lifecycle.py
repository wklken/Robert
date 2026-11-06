"""Task lifecycle transitions shared by execution and publication."""
import json
from robert_agent import native_rounds, remediation, work_items
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
    conn.execute(
        "UPDATE tasks SET lifecycle = 'failed', updated_at = ? WHERE task_id = ?",
        (run_now, task_id),
    )
    conn.execute(
        """
        UPDATE workstreams
        SET lifecycle = 'failed', active_task_id = NULL, updated_at = ?,
            metadata_json = ?
        WHERE workstream_id = ?
        """,
        (
            run_now,
            json.dumps({"last_audit": audit}, ensure_ascii=False, sort_keys=True),
            workstream_id,
        ),
    )
    remediation.record_task_failure(
        conn,
        task_id=task_id,
        reason=(
            audit.get("status")
            if isinstance(audit, dict)
            else "task_failed"
        )
        or "task_failed",
        now=run_now,
    )
    if not (isinstance(audit, dict) and audit.get("status") == "failed_timeout"):
        execution_ownership.close_terminal_task(conn, task_id, run_now)
    return True


def mark_task_waiting_for_publish(conn, task_id, workstream_id, run_now):
    conn.execute(
        "UPDATE tasks SET lifecycle = 'running', updated_at = ? WHERE task_id = ?",
        (run_now, task_id),
    )
    conn.execute(
        """
        UPDATE workstreams
        SET lifecycle = 'active', active_task_id = ?, updated_at = ?
        WHERE workstream_id = ?
        """,
        (task_id, run_now, workstream_id),
    )


def mark_task_waiting_for_user(conn, task_id, workstream_id, run_now):
    conn.execute(
        "UPDATE tasks SET lifecycle = 'waiting_for_user', updated_at = ? WHERE task_id = ?",
        (run_now, task_id),
    )
    conn.execute(
        """
        UPDATE workstreams
        SET lifecycle = 'waiting_for_user', active_task_id = ?, updated_at = ?
        WHERE workstream_id = ?
        """,
        (task_id, run_now, workstream_id),
    )


def finalize_superseded_result(conn, result_id, now):
    row = conn.execute("SELECT wr.task_id,t.workstream_id,t.task_kind FROM worker_results wr JOIN tasks t ON t.task_id=wr.task_id WHERE wr.result_id=?", (result_id,)).fetchone()
    if not row or row[2] != 'human_request' or not execution_ownership.claim_task(conn, row[0], now):
        return False
    actions = conn.execute("SELECT publish_status,metadata_json FROM github_actions WHERE result_id=?", (result_id,)).fetchall()
    if not actions or any(status not in {'published', 'skipped'} for status, _ in actions):
        return False
    if not any(status == 'skipped' and _json_object(encoded).get('publish', {}).get('reason_code') == 'superseded' for status, encoded in actions):
        return False
    finalize_failed_task(conn, row[0], row[1], now, {'status': 'publication_superseded',
        'safe_error': 'The remote branch changed. Retry from the current head.'})
    item = work_items.resolve_work_item_for_workstream(conn, row[1])
    if item:
        work_items.record_system_event(conn, item['work_item_id'], event_type='execution_failed',
            idempotency_key='superseded:' + result_id, body='The remote branch changed. Retry from the current head.',
            metadata={'task_id': row[0], 'result_id': result_id}, now=now)
    conn.execute("UPDATE wakeups SET status='consumed',updated_at=? WHERE result_id=? AND status='pending'", (now, result_id))
    return True
