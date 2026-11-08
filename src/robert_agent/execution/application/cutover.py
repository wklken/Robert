"""Single-protocol guard and isolation of incompatible task evidence."""

from robert_agent.foundation.contracts.execution import AttemptStatus
from robert_agent.foundation.contracts.work import TaskKind, TaskLifecycle

import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from robert_agent.adapters.processes import attempt as attempt_process
from robert_agent.delivery import contract as result_contract
from robert_agent.execution import spool as result_spool
from robert_agent.persistence import database as db_database
from robert_agent.persistence import errors as db_errors
from robert_agent.persistence import records as db_records
from robert_agent.persistence import transactions as db_transactions
from robert_agent.persistence.queries import attempts as db_read_attempts
from robert_agent.persistence.queries import result_contract as contract_queries
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.stores import attempts as db_write_attempts
from robert_agent.persistence.stores import notifications as db_write_notifications
from robert_agent.persistence.stores import tasks as db_write_tasks

RECOVERY = (
    "Inspect the isolated execution and confirm its workers stopped. Reconcile pending publications. "
    "For GitHub tasks, use Retrigger to create a replacement with refreshed context. "
    "Alternatively restart the previous version to finish prepared/running workers, "
    "ingest its spool and reconcile all pending publications; verify worker processes stopped. "
    "Alternatively stop and reconcile the affected work, then use Workbench Cancel and "
    "create a replacement task on the new version with the remaining approved scope. "
    "Canceled work items cannot be retried. Never delete DB/spool evidence or run an old prompt on this version."
)


def inspect(conn, db_path):
    blockers = []
    attempts, results = contract_queries.legacy_evidence(conn, result_contract.CONTRACT_ID)
    for attempt, task, status, metadata, lifecycle in attempts:
        if json.loads(metadata or "{}").get("result_contract") == result_contract.CONTRACT_ID:
            continue
        if status in {AttemptStatus.RUNNING.value, AttemptStatus.STALE.value} or (
            status == AttemptStatus.PREPARED.value
            and lifecycle in {TaskLifecycle.QUEUED.value, TaskLifecycle.RUNNING.value}
        ):
            blockers.append({"kind": "legacy_" + status, "task_id": task, "attempt_id": attempt})
    for result, task in results:
        blockers.append({"kind": "legacy_unresolved_result", "task_id": task, "result_id": result})
    root = result_spool.directory(db_path)
    for path in sorted(root.glob("*.json")):
        try:
            record = json.loads(path.read_text())
            result_spool._validate_record(record)
        except FileNotFoundError:
            continue  # Concurrent ingestion already removed this pending record.
        except (ValueError, OSError, TypeError, KeyError):
            blockers.append({"kind": "legacy_or_invalid_spool", "path": str(path)})
    # Rejected old formats are preserved under this extension by the replay reader.
    blockers.extend(
        {"kind": "unreconciled_invalid_spool", "path": str(path)} for path in sorted(root.glob("*.invalid"))
    )
    # Quarantined tasks cannot run, consume results or publish. Their evidence
    # remains intact, but it must not prevent unrelated tasks from proceeding.
    return {
        "ok": not blockers,
        "status": "ready" if not blockers else "blocked_result_contract_cutover",
        "result_contract": result_contract.CONTRACT_ID,
        "blockers": blockers,
        **({"required_action": RECOVERY} if blockers else {}),
    }


def check(db_path):
    path = Path(db_path)
    if not path.exists():
        return {"ok": True, "status": "ready", "blockers": []}
    try:
        with closing(db_database.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
            return inspect(conn, path)
    except (db_errors.Error, ValueError, OSError, TypeError):
        return {
            "ok": False,
            "status": "blocked_result_contract_cutover",
            "safe_error": "Cannot inspect result contract cutover state.",
            "required_action": RECOVERY,
        }


def prepare(db_path, *, now=None, isolated_only=False):
    try:
        return _prepare(db_path, now=now, isolated_only=isolated_only)
    except (db_errors.Error, ValueError, OSError, TypeError):
        return {
            "ok": False,
            "status": "blocked_result_contract_cutover",
            "safe_error": "Cannot safely isolate incompatible execution state.",
            "required_action": RECOVERY,
        }


def _prepare(db_path, *, now=None, isolated_only=False):
    """Isolate identified task blockers; unknown spool/read failures stay global.

    This never converts an old result or attests that a live worker stopped.
    Cancellation and replacement remain explicit operator actions.
    """
    from robert_agent.delivery.lock import task_publication_lock
    from robert_agent.execution import rounds as native_rounds
    from robert_agent.persistence import execution_control as execution_store
    from robert_agent.work.application import commands as work_items

    now = now or datetime.now(timezone.utc).isoformat()
    checked = {"blockers": []} if isolated_only else check(db_path)
    if not Path(db_path).exists():
        return {"ok": True, "status": "ready", "blockers": []}
    task_ids = {item["task_id"] for item in checked.get("blockers", []) if item.get("task_id")}
    # Isolation prevents admission and result consumption, but exit observation
    # must continue until every recorded process has released its capacity.
    with closing(db_database.connect(db_path, timeout=5)) as reader:
        task_ids.update(row[0] for row in db_read_attempts.find_for_prepare(reader))
    for task_id in sorted(task_ids):
        with task_publication_lock(db_path, task_id) as acquired:
            if not acquired:
                continue
            with closing(db_database.connect(db_path, timeout=5)) as conn:
                snapshots = list(db_read_attempts.find_for_prepare_2(conn, task_id))
                stopped = []
                # Observe processes outside a writer transaction. Retain occupied
                # capacity whenever identity or descendant cleanup is uncertain.
                for attempt in snapshots:
                    metadata = json.loads(attempt["metadata_json"] or "{}")
                    if attempt_process.dispatch_status(metadata)["status"] == "dead":
                        stopped.append(attempt)
                native_exit = native_rounds.observe_exit(conn, task_id)
                with conn:
                    db_transactions.begin(conn, immediate=True)
                    task = db_records.first(db_read_tasks.find_record_by_task_id(conn, task_id))
                    if not task:
                        continue
                    isolated = (
                        json.loads(task["metadata_json"] or "{}").get("execution_attention", {}).get("reason_code")
                        == "result_contract_cutover"
                    )
                    if not isolated:
                        if task["lifecycle"] in {TaskLifecycle.CANCELED, TaskLifecycle.IGNORED} or isolated_only:
                            continue
                        if not any(item.get("task_id") == task_id for item in inspect(conn, db_path)["blockers"]):
                            continue
                    attention = {
                        "reason_code": "result_contract_cutover",
                        "retryable": False,
                        "required_action": RECOVERY,
                    }
                    if not isolated:
                        db_write_tasks.update_for_prepare(conn, json.dumps(attention), now, task_id)
                        if task["task_kind"] == TaskKind.HUMAN_REQUEST and task["lifecycle"] not in {
                            TaskLifecycle.COMPLETED,
                            TaskLifecycle.FAILED,
                        }:
                            execution_store.ensure_subject(conn, "task", task_id, TaskKind.HUMAN_REQUEST.value, now)
                            execution_store.mark_binding_attention(conn, "task", task_id, now)
                    current_native = native_rounds.release_observed_slots(conn, task_id, native_exit)
                    if not current_native or not native_exit["all_stopped"]:
                        stopped = []
                    for attempt in stopped:
                        # A completed result remains historical evidence. Only
                        # mark its process stopped; do not rewrite its outcome.
                        if attempt["status"] == AttemptStatus.COMPLETED:
                            db_write_attempts.update_for_prepare_2(
                                conn, now, attempt["attempt_id"], attempt["status"], attempt["metadata_json"]
                            )
                            continue
                        db_write_attempts.update_for_prepare(
                            conn,
                            now,
                            json.dumps(attention),
                            now,
                            attempt["attempt_id"],
                            attempt["status"],
                            attempt["metadata_json"],
                        )
                    if isolated:
                        continue  # Existing attention remains until explicit recovery.
                    db_write_notifications.insert_for_prepare(
                        conn, "notification-cutover-" + task_id, task_id, now, json.dumps(attention)
                    )
                    item = work_items.resolve_work_item_for_workstream(conn, task["workstream_id"])
                    if item:
                        work_items.record_system_event(
                            conn,
                            item["work_item_id"],
                            event_type="operator_decision_required",
                            idempotency_key="result-contract-cutover:" + task_id,
                            body="Automatic execution stopped: incompatible worker result. " + RECOVERY,
                            metadata={
                                "kind": "result_contract_cutover",
                                "task_id": task_id,
                                "notification_id": "notification-cutover-" + task_id,
                            },
                            now=now,
                        )
    return {"ok": True, "status": "isolated_processes_observed"} if isolated_only else check(db_path)
