"""Transactional commands for repository-scoped DD work items."""

from robert_agent.foundation.contracts.execution import BindingStatus
from robert_agent.foundation.contracts.work import (
    ACTIVE_TASK_LIFECYCLES,
    PUBLICATION_RETRY_WAKEUP_KIND,
    TaskLifecycle,
    WorkCommand,
    WorkstreamLifecycle,
)

from robert_agent.persistence import database as db_database
from robert_agent.persistence import transactions as db_transactions
from robert_agent.persistence.dialects import sqlite as db_sqlite

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import attempts as db_read_attempts
from robert_agent.persistence.queries import github_actions as db_read_github_actions
from robert_agent.persistence.queries import publication_intents as db_read_publication_intents
from robert_agent.persistence.queries import repos as db_read_repos
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import wakeups as db_read_wakeups
from robert_agent.persistence.queries import work_item_events as db_read_work_item_events
from robert_agent.persistence.queries import work_items as db_read_work_items
from robert_agent.persistence.queries import worker_results as db_read_worker_results
from robert_agent.persistence.queries import workstreams as db_read_workstreams
from robert_agent.persistence.stores import attempts as db_write_attempts
from robert_agent.persistence.stores import notifications as db_write_notifications
from robert_agent.persistence.stores import tasks as db_write_tasks
from robert_agent.persistence.stores import work_item_events as db_write_work_item_events
from robert_agent.persistence.stores import work_items as db_write_work_items
from robert_agent.persistence.stores import worker_results as db_write_worker_results
from robert_agent.persistence.stores import workstreams as db_write_workstreams
from robert_agent.work.inputs import (
    COMMANDS,
    CommandContext,
    DECISION_EVENTS,
    FAILURE_EVENTS,
    MAX_DESCRIPTION,
    MAX_REPLY,
    MAX_TITLE,
    QUESTION_EVENTS,
    validate_idempotency_key,
    validate_priority,
    validate_routing,
    validate_text,
)
from robert_agent.work import control_queries

from robert_agent.work.errors import WorkItemValidationError, WorkItemConflictError, WorkItemNotFoundError
from contextlib import closing, ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from uuid import uuid4

from robert_agent.supervision import lineage as supervision_lineage
from robert_agent.work import wakeup
from robert_agent.adapters.notifications import wakeup as wakeup_notifier
from robert_agent.work.application import control as work_item_control


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _decode_json(value, default):
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return default
    return parsed


def _item_from_row(row):
    if not hasattr(row, "keys"):
        columns = (
            "work_item_id",
            "repo_id",
            "title",
            "description",
            "priority",
            "origin_type",
            "origin_source_id",
            "routing_mode",
            "requested_worker",
            "workstream_id",
            "creation_idempotency_key",
            "created_by",
            "activated_at",
            "completed_at",
            "canceled_at",
            "version",
            "created_at",
            "updated_at",
            "metadata_json",
        )
        row = dict(zip(columns, row))
    return {
        "work_item_id": row["work_item_id"],
        "repo_id": row["repo_id"],
        "title": row["title"],
        "description": row["description"],
        "priority": row["priority"],
        "origin_type": row["origin_type"],
        "origin_source_id": row["origin_source_id"],
        "routing_mode": row["routing_mode"],
        "requested_worker": row["requested_worker"],
        "workstream_id": row["workstream_id"],
        "created_by": row["created_by"],
        "activated_at": row["activated_at"],
        "completed_at": row["completed_at"],
        "canceled_at": row["canceled_at"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _load_item(conn, work_item_id):
    row = db_records.first(db_read_work_items.find_record_by_work_item_id(conn, work_item_id))
    if not row:
        raise WorkItemNotFoundError("work item not found")
    return row


def _event_from_row(row):
    if not hasattr(row, "keys"):
        columns = (
            "event_id",
            "work_item_id",
            "event_type",
            "actor_kind",
            "actor_identity",
            "body",
            "resolves_event_id",
            "idempotency_key",
            "created_at",
            "metadata_json",
        )
        row = dict(zip(columns, row))
    return {
        "event_id": row["event_id"],
        "work_item_id": row["work_item_id"],
        "event_type": row["event_type"],
        "actor_kind": row["actor_kind"],
        "actor_identity": row["actor_identity"],
        "body": row["body"],
        "resolves_event_id": row["resolves_event_id"],
        "idempotency_key": row["idempotency_key"],
        "created_at": row["created_at"],
        "metadata": _decode_json(row["metadata_json"], {}) or {},
    }


def _insert_event(
    conn,
    work_item_id,
    *,
    event_type,
    actor_kind,
    actor_identity,
    idempotency_key,
    body="",
    resolves_event_id=None,
    metadata=None,
    now=None,
):
    now = now or utc_now()
    event_id = f"wie-{uuid4().hex}"
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
    db_write_work_item_events.insert_event(
        conn,
        event_id,
        work_item_id,
        event_type,
        actor_kind,
        actor_identity,
        body,
        resolves_event_id,
        idempotency_key,
        now,
        metadata_json,
    )
    return {
        "event_id": event_id,
        "work_item_id": work_item_id,
        "event_type": event_type,
        "actor_kind": actor_kind,
        "actor_identity": actor_identity,
        "body": body,
        "resolves_event_id": resolves_event_id,
        "idempotency_key": idempotency_key,
        "created_at": now,
        "metadata": metadata or {},
    }


def record_system_event(
    conn,
    work_item_id,
    *,
    event_type,
    idempotency_key,
    body="",
    resolves_event_id=None,
    metadata=None,
    now=None,
):
    validate_idempotency_key(idempotency_key)
    _load_item(conn, work_item_id)
    existing = db_records.first(db_read_work_item_events.record_system_event(conn, work_item_id, idempotency_key))
    if existing:
        return _event_from_row(existing)
    return _insert_event(
        conn,
        work_item_id,
        event_type=event_type,
        actor_kind="system",
        actor_identity="robert",
        idempotency_key=idempotency_key,
        body=body,
        resolves_event_id=resolves_event_id,
        metadata=metadata,
        now=now,
    )


def resolve_work_item_for_workstream(conn, workstream_id):
    current = workstream_id
    visited = set()
    while current and current not in visited:
        visited.add(current)
        row = db_records.first(db_read_work_items.find_record_by_workstream_id(conn, current))
        if row:
            return _item_from_row(row)
        stream = db_records.first(db_read_workstreams.find_origin_workstream_id_by_workstream_id(conn, current))
        current = stream[0] if stream else None
    return None


def ensure_github_work_item(
    conn,
    *,
    repo_id,
    source_id,
    workstream_id,
    actor_identity,
    route_confidence,
    now,
):
    existing = resolve_work_item_for_workstream(conn, workstream_id)
    if existing:
        return existing

    root_workstream_id = workstream_id
    visited = set()
    while root_workstream_id not in visited:
        visited.add(root_workstream_id)
        row = db_records.first(db_read_workstreams.find_origin_workstream_id_by_workstream_id(conn, root_workstream_id))
        if not row or not row[0]:
            break
        root_workstream_id = row[0]

    root = db_records.first(db_read_workstreams.find_for_ensure_github_work_item(conn, root_workstream_id))
    root_source_id = (root[0] if root else None) or source_id
    duplicate = db_records.first(db_read_work_items.find_record_by_origin_source_id(conn, root_source_id))
    if duplicate:
        return _item_from_row(duplicate)

    digest = hashlib.sha256(root_workstream_id.encode("utf-8")).hexdigest()[:20]
    work_item_id = f"wi-{digest}"
    title = (root[2] if root else None) or f"Workstream {root_workstream_id}"
    created_by = actor_identity or (root[3] if root else None) or "system"
    created_at = (root[1] if root else None) or now
    activated_at = now if route_confidence == "high" else None
    db_write_work_items.insert_for_ensure_github_work_item(
        conn,
        work_item_id,
        repo_id,
        title,
        root_source_id,
        root_workstream_id,
        f"github:{root_source_id}",
        created_by,
        activated_at,
        created_at,
        now,
    )
    item = db_records.first(
        db_read_work_items.find_for_ensure_github_work_item(conn, root_workstream_id, root_source_id)
    )
    if not item:
        raise WorkItemConflictError("GitHub work item identity conflicts with existing data")
    item_payload = _item_from_row(item)
    event_type = "github_intake" if route_confidence == "high" else "operator_decision_required"
    _insert_event(
        conn,
        item_payload["work_item_id"],
        event_type=event_type,
        actor_kind="github",
        actor_identity=created_by,
        idempotency_key=f"github-intake:{root_source_id}",
        metadata={
            "route_confidence": route_confidence,
            "source_id": source_id,
            "workstream_id": workstream_id,
        },
        now=now,
    )
    return item_payload


def _stored_command_result(conn, work_item_id, idempotency_key):
    row = db_records.first(db_read_work_item_events.find_for_stored_command_result(conn, work_item_id, idempotency_key))
    if not row:
        return None
    metadata = _decode_json(row[0], {}) or {}
    return metadata.get("command_result")


def _unresolved_event(conn, work_item_id, event_types):
    return db_records.first(db_read_work_item_events.unresolved_event_filtered(conn, event_types, work_item_id))


def record_github_response(
    conn,
    workstream_id,
    *,
    actor_identity,
    body,
    event_fingerprint,
    metadata=None,
    now=None,
):
    item = resolve_work_item_for_workstream(conn, workstream_id)
    if not item:
        return None
    question = _unresolved_event(conn, item["work_item_id"], QUESTION_EVENTS)
    if not question:
        return None
    idempotency_key = "github-response:" + hashlib.sha256(event_fingerprint.encode("utf-8")).hexdigest()[:24]
    existing = db_records.first(
        db_read_work_item_events.record_system_event(conn, item["work_item_id"], idempotency_key)
    )
    if existing:
        return _event_from_row(existing)
    now = now or utc_now()
    response = _insert_event(
        conn,
        item["work_item_id"],
        event_type="user_response",
        actor_kind="github",
        actor_identity=actor_identity,
        idempotency_key=idempotency_key,
        body=body,
        resolves_event_id=(question["event_id"] if hasattr(question, "keys") else question[0]),
        metadata={"event_fingerprint": event_fingerprint, **(metadata or {})},
        now=now,
    )
    db_write_work_items.record_github_response(conn, now, item["work_item_id"])
    return response


def _has_open_pull_request(conn, workstream_id):
    if not workstream_id:
        return False
    return bool(
        db_records.first(db_read_workstreams.find_for_has_open_pull_request(conn, workstream_id, workstream_id))
    )


def _active_or_latest_task(conn, workstream_id):
    row = db_records.first(db_read_workstreams.find_for_active_or_latest_task(conn, workstream_id))
    if row:
        return row
    return db_records.first(db_read_tasks.find_record_by_workstream_id(conn, workstream_id))


def _create_task(
    conn,
    item,
    *,
    parent_task_id=None,
    routing_mode=None,
    requested_worker=None,
    trigger_event_id=None,
    now,
    workstream_id=None,
    lineage_key=None,
):
    workstream_id = workstream_id or item["workstream_id"]
    task_id = f"task-{uuid4().hex}"
    routing_mode = routing_mode or item["routing_mode"]
    if routing_mode == "auto":
        requested_worker = None
    elif requested_worker is None:
        requested_worker = item["requested_worker"]
    metadata = {
        "origin_type": item["origin_type"],
        "work_item_id": item["work_item_id"],
    }
    repair = (_decode_json(item["metadata_json"], {}) or {}).get("supervision_repair")
    if repair and repair.get("automatic") is True:
        routing_mode, requested_worker = "manual", repair["worker"]
        metadata["supervision_repair"] = repair
    if trigger_event_id:
        metadata["trigger_work_item_event_id"] = trigger_event_id
    if lineage_key:
        metadata[supervision_lineage.LINEAGE_FIELD] = lineage_key
    db_write_tasks.insert_for_create_task(
        conn,
        task_id,
        workstream_id,
        parent_task_id,
        item["priority"],
        routing_mode,
        requested_worker,
        now,
        now,
        json.dumps(metadata, sort_keys=True),
    )
    db_write_workstreams.update_for_create_task(conn, task_id, now, workstream_id)
    wakeup.request_wakeup(
        conn,
        repo_id=item["repo_id"],
        reason="manual_operator_request",
        dedupe_key=f"work-item-event:{trigger_event_id or task_id}",
        work_item_id=item["work_item_id"],
        task_id=task_id,
        metadata={"work_item_id": item["work_item_id"]},
        now=now,
    )
    return task_id


def _commit_result(conn, db_path, result, notifier):
    conn.commit()
    if result.get("task_id"):
        wakeup_notifier.notify(db_path, sender=notifier)
    return result


def retrigger_failed_github_task(
    db_path,
    task_id,
    *,
    context,
    idempotency_key,
    notifier=None,
):
    idempotency_key = validate_idempotency_key(idempotency_key)
    db_path = Path(db_path).expanduser()
    # Refresh missing source content outside the write transaction. Preserve
    # the original event; the new prompt receives an audited context snapshot.
    refreshed_context = None
    recovery_snapshot = None
    with closing(db_database.connect(db_path)) as reader:
        snapshot = db_records.first(db_read_tasks.find_for_retrigger_failed_github_task(reader, task_id))
        if snapshot and snapshot["repo_id"] in context.allowed_repo_ids:
            attention = _decode_json(snapshot["metadata_json"], {}).get("execution_attention", {})
            if attention.get("reason_code") == "result_contract_cutover":
                recovery_snapshot = snapshot["metadata_json"]
                from robert_agent.adapters.github import discovery as discover
                from robert_agent.work.queries import task_related_events

                refreshed_context = task_related_events(reader, task_id)
                refreshed = []
                for event in refreshed_context:
                    updated, state = discover._source_metadata(event, dict(snapshot), subprocess.run)
                    if state is None:
                        raise WorkItemConflictError("GitHub source refresh failed; retry after access is restored")
                    if state.lower() != "open":
                        raise WorkItemConflictError("retrigger requires an open GitHub source")
                    refreshed.append(updated)
                if not refreshed:
                    raise WorkItemConflictError("source context is unavailable; inspect the original task")
                refreshed_context = refreshed
    from robert_agent.delivery.lock import task_publication_lock

    with ExitStack() as locks, closing(db_database.connect(db_path, isolation_level=None)) as conn:
        if not locks.enter_context(task_publication_lock(db_path, task_id)):
            raise WorkItemConflictError("A publication is in progress; retry after it finishes")
        db_sqlite.configure(conn, foreign_keys=True)
        db_sqlite.configure(conn, busy_timeout_ms=5000)
        from robert_agent.execution import rounds as native_rounds

        native_exit = native_rounds.observe_exit(conn, task_id)
        db_transactions.begin(conn, immediate=True)
        try:
            row = db_records.first(db_read_tasks.find_for_retrigger_failed_github_task_2(conn, task_id))
            if not row or row["repo_id"] not in context.allowed_repo_ids:
                raise WorkItemNotFoundError("task not found")
            from robert_agent.persistence import execution_control as execution_store

            binding = execution_store.get_binding(conn, "task", task_id)
            attention = _decode_json(row["metadata_json"], {}).get("execution_attention", {})
            cutover_recovery = attention.get("reason_code") == "result_contract_cutover"
            if cutover_recovery and recovery_snapshot != row["metadata_json"]:
                raise WorkItemConflictError("execution state changed; refresh before recovery")
            if row["lifecycle"] != TaskLifecycle.FAILED.value and not cutover_recovery:
                raise WorkItemConflictError("only a failed GitHub task can be retriggered")
            if row["source_state"].lower() != "open":
                raise WorkItemConflictError("retrigger requires an open GitHub source")
            if row["active_task_id"] is not None and not (cutover_recovery and row["active_task_id"] == task_id):
                raise WorkItemConflictError("the workstream already has an active task")
            latest = db_records.first(db_read_tasks.find_task_id_by_workstream_id(conn, row["workstream_id"]))
            if not latest or latest[0] != task_id:
                raise WorkItemConflictError("only the latest failed GitHub task can be retriggered")

            # Explicit recovery creates a new subject; never reopen or transfer
            # ownership of the old workflow, nor overlap an uncertain process.
            if db_records.first(db_read_attempts.find_for_retrigger_failed_github_task(conn, task_id)):
                raise WorkItemConflictError("worker termination must be confirmed before retry")
            if db_records.first(db_read_github_actions.find_for_retrigger_failed_github_task(conn, task_id)):
                raise WorkItemConflictError("pending publication must be reconciled or canceled before retry")
            failed_attempt = db_records.first(db_read_attempts.find_metadata_json_by_task_id(conn, task_id))
            if failed_attempt:
                process_metadata = json.loads(failed_attempt[0] or "{}")
                if process_metadata.get("dispatch"):
                    from robert_agent.adapters.processes.attempt import dispatch_status

                    if dispatch_status(process_metadata)["status"] != "dead":
                        raise WorkItemConflictError("worker termination must be confirmed before retry")
            if cutover_recovery:
                if not native_exit["all_stopped"] or not native_rounds.exit_observation_current(
                    conn, task_id, native_exit
                ):
                    raise WorkItemConflictError("Review workers must be reconciled before recovery")
                recovery_now = utc_now()
                work_item_control.require_idle_repository(conn, row["repo_id"], recovery_now)
                metadata = _decode_json(row["metadata_json"], {})
                metadata["recovered_execution_attention"] = {
                    **metadata.pop("execution_attention"),
                    "recovered_at": recovery_now,
                    "requested_by": context.actor_identity,
                }
                db_write_tasks.update_for_retrigger_failed_github_task(
                    conn, json.dumps(metadata), recovery_now, task_id
                )
                db_write_workstreams.update_for_supervise_running_attempts(conn, recovery_now, row["workstream_id"])
                item = resolve_work_item_for_workstream(conn, row["workstream_id"])
                if item:
                    unresolved = db_records.first(
                        db_read_work_item_events.find_for_retrigger_failed_github_task(
                            conn, item["work_item_id"], "result-contract-cutover:" + task_id
                        )
                    )
                    _insert_event(
                        conn,
                        item["work_item_id"],
                        event_type="execution_recovery_requested",
                        actor_kind=context.actor_kind,
                        actor_identity=context.actor_identity,
                        idempotency_key="cutover-recovery:" + hashlib.sha256(idempotency_key.encode()).hexdigest(),
                        resolves_event_id=unresolved["event_id"] if unresolved else None,
                        body="Replace incompatible execution using refreshed GitHub context; preserve original evidence.",
                        metadata={"task_id": task_id, "kind": "result_contract_cutover"},
                        now=recovery_now,
                    )
                db_write_notifications.update_for_persist_control_failure_alerts(
                    conn, "notification-cutover-" + task_id
                )
            if binding and binding["binding_status"] != BindingStatus.TERMINAL:
                execution_store.close_binding(conn, "task", task_id, utc_now())

            dedupe_key = f"github-retrigger:{idempotency_key}"
            existing = db_records.first(
                db_read_wakeups.find_for_retrigger_failed_github_task(conn, row["repo_id"], task_id)
            )
            if existing and existing[0] != dedupe_key:
                raise WorkItemConflictError("a retrigger request is already pending")

            requested = wakeup.request_wakeup(
                conn,
                repo_id=row["repo_id"],
                reason="manual_operator_request",
                dedupe_key=dedupe_key,
                task_id=task_id,
                metadata={
                    "kind": "github_retrigger",
                    "parent_task_id": task_id,
                    "requested_by": context.actor_identity,
                    "workstream_id": row["workstream_id"],
                    **({"refreshed_context": refreshed_context} if refreshed_context is not None else {}),
                },
            )
            should_notify = requested.get("status") != "already_pending"
            result = {
                "ok": True,
                "status": "queued",
                "task_id": task_id,
                "wakeup_id": requested["wakeup_id"],
            }
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if should_notify:
        wakeup_notifier.notify(db_path, sender=notifier)
    return result


def _activate(conn, item, context, idempotency_key, now, *, initial_version=None):
    if item["origin_type"] != "web" or item["activated_at"] is not None:
        raise WorkItemConflictError("only a web Backlog item can be started")
    workstream_id = f"local:{item['work_item_id']}"
    version = initial_version if initial_version is not None else item["version"] + 1
    db_write_workstreams.insert_for_activate(
        conn,
        workstream_id,
        item["repo_id"],
        now,
        now,
        json.dumps({"origin_type": "web", "work_item_id": item["work_item_id"]}),
    )
    db_write_work_items.update_for_activate(conn, workstream_id, now, now, version, item["work_item_id"])
    item = _load_item(conn, item["work_item_id"])
    task_id = _create_task(
        conn, item, now=now, lineage_key=supervision_lineage.operator_key(item["work_item_id"], idempotency_key)
    )
    result = {
        "ok": True,
        "command": "start",
        "item": _item_from_row(_load_item(conn, item["work_item_id"])),
        "task_id": task_id,
    }
    activation = _insert_event(
        conn,
        item["work_item_id"],
        event_type="activated",
        actor_kind=context.actor_kind,
        actor_identity=context.actor_identity,
        idempotency_key=idempotency_key,
        metadata={"command_result": result, "task_id": task_id},
        now=now,
    )
    supervision_lineage.record_operator_admission(conn, item, activation, task_id, now)
    _insert_event(
        conn,
        item["work_item_id"],
        event_type="routing_selected",
        actor_kind=context.actor_kind,
        actor_identity=context.actor_identity,
        idempotency_key=f"{idempotency_key}:routing",
        metadata={
            "routing_mode": item["routing_mode"],
            "requested_worker": item["requested_worker"],
            "task_id": task_id,
        },
        now=now,
    )
    return result


def _insert_work_item(
    conn,
    *,
    context,
    repo_id,
    title,
    description,
    priority,
    routing_mode,
    requested_worker,
    start,
    idempotency_key,
    now,
    metadata=None,
):
    metadata = dict(metadata or {})
    work_item_id = f"wi-{uuid4().hex}"
    db_write_work_items.insert_work_item(
        conn,
        work_item_id,
        repo_id,
        title,
        description,
        priority,
        routing_mode,
        requested_worker,
        idempotency_key,
        context.actor_identity,
        now,
        now,
        json.dumps(metadata, sort_keys=True),
    )
    _insert_event(
        conn,
        work_item_id,
        event_type="created",
        actor_kind=context.actor_kind,
        actor_identity=context.actor_identity,
        idempotency_key=f"create:{idempotency_key}",
        now=now,
    )
    item = _load_item(conn, work_item_id)
    if start:
        result = _activate(conn, item, context, f"start:{idempotency_key}", now, initial_version=1)
        result["command"] = "create_and_start"
    else:
        result = {"ok": True, "command": "create", "item": _item_from_row(item)}
    metadata["command_result"] = result
    db_write_work_items.set_metadata_json_by_work_item_id(
        conn, json.dumps(metadata, ensure_ascii=False, sort_keys=True), work_item_id
    )
    return result


def create_supervision_repair(conn, *, repo_id, title, description, repair, now, idempotency_key):
    """Caller-owned transaction for a policy-authorized system maintenance item."""
    validate_idempotency_key(idempotency_key)
    title = validate_text(title, "title", MAX_TITLE, required=True)
    description = validate_text(description, "description", MAX_DESCRIPTION)
    if not db_records.first(db_read_repos.find_for_create_supervision_repair(conn, repo_id)):
        raise WorkItemValidationError("maintenance repository is not enabled")
    existing = db_records.first(db_read_work_items.find_record_by_creation_idempotency_key(conn, idempotency_key))
    if existing:
        metadata = _decode_json(existing["metadata_json"], {}) or {}
        if (
            existing["repo_id"] != repo_id
            or existing["created_by"] != "system:supervisor"
            or (metadata.get("supervision_repair") or {}).get("root_key") != repair["root_key"]
        ):
            raise WorkItemConflictError("maintenance item idempotency key conflicts")
        return metadata["command_result"], False
    start = repair.get("automatic") is True
    worker = repair.get("worker") if start else None
    context = CommandContext("system", "system:supervisor", frozenset({repo_id}), frozenset({worker} if worker else ()))
    routing_mode, worker = validate_routing(context, "manual" if start else "auto", worker)
    return _insert_work_item(
        conn,
        context=context,
        repo_id=repo_id,
        title=title,
        description=description,
        priority="P2",
        routing_mode=routing_mode,
        requested_worker=worker,
        start=start,
        idempotency_key=idempotency_key,
        now=now,
        metadata={"supervision_repair": repair},
    ), True


def create_work_item(
    db_path,
    *,
    context,
    repo_id,
    title,
    description,
    priority,
    routing_mode,
    requested_worker,
    start,
    idempotency_key,
    notifier=None,
):
    validate_idempotency_key(idempotency_key)
    if repo_id not in context.allowed_repo_ids:
        raise WorkItemValidationError("repository is not enabled")
    title = validate_text(title, "title", MAX_TITLE, required=True)
    description = validate_text(description, "description", MAX_DESCRIPTION)
    priority = validate_priority(priority)
    routing_mode, requested_worker = validate_routing(
        context,
        routing_mode,
        requested_worker,
    )
    if not isinstance(start, bool):
        raise WorkItemValidationError("start must be a boolean")

    db_path = Path(db_path).expanduser()
    with closing(db_database.connect(db_path, isolation_level=None)) as conn:
        db_sqlite.configure(conn, foreign_keys=True)
        db_sqlite.configure(conn, busy_timeout_ms=5000)
        db_transactions.begin(conn, immediate=True)
        try:
            existing = db_records.first(
                db_read_work_items.find_record_by_creation_idempotency_key(conn, idempotency_key)
            )
            if existing:
                metadata = _decode_json(existing["metadata_json"], {}) or {}
                result = metadata.get("command_result")
                if result:
                    return _commit_result(conn, db_path, result, notifier)
                result = {"ok": True, "command": "create", "item": _item_from_row(existing)}
                return _commit_result(conn, db_path, result, notifier)

            result = _insert_work_item(
                conn,
                context=context,
                repo_id=repo_id,
                title=title,
                description=description,
                priority=priority,
                routing_mode=routing_mode,
                requested_worker=requested_worker,
                start=start,
                idempotency_key=idempotency_key,
                now=utc_now(),
            )
            return _commit_result(conn, db_path, result, notifier)
        except Exception:
            conn.rollback()
            raise


def _finish_parent_for_child(conn, parent_task, now):
    if not parent_task:
        return None
    if parent_task["lifecycle"] in {TaskLifecycle.WAITING_FOR_USER, TaskLifecycle.FAILED}:
        db_write_tasks.update_for_finish_parent_for_child(conn, now, parent_task["task_id"])
    return parent_task["task_id"]


def _command_event_type(command):
    return {
        "edit": "edited",
        "approve": "approved",
        "reply": "user_response",
        "request_changes": "changes_requested",
        "retry": "retry_requested",
        "retry_publish": "publication_retry_requested",
        "cancel": "canceled",
        "reopen": "reopened",
    }[command]


def execute_command(
    db_path,
    work_item_id,
    *,
    context,
    command,
    expected_version,
    expected_event_id=None,
    idempotency_key,
    body="",
    routing_mode=None,
    requested_worker=None,
    title=None,
    description=None,
    priority=None,
    notifier=None,
    abandon_review_run_ids=None,
):
    validate_idempotency_key(idempotency_key)
    if command not in COMMANDS:
        raise WorkItemValidationError("unknown work item command")
    if not isinstance(expected_version, int) or isinstance(expected_version, bool):
        raise WorkItemValidationError("expected_version must be an integer")
    if expected_event_id is not None:
        expected_event_id = validate_text(expected_event_id, "expected_event_id", 128, required=True)
    body = validate_text(body, "body", MAX_REPLY)
    if abandon_review_run_ids is None:
        abandon_review_run_ids = []
    if not isinstance(abandon_review_run_ids, list):
        raise WorkItemValidationError("abandon_review_run_ids must be a list of run IDs")
    abandon_review_run_ids = [
        validate_text(value, "review run ID", 128, required=True) for value in abandon_review_run_ids
    ]
    if len(set(abandon_review_run_ids)) != len(abandon_review_run_ids):
        raise WorkItemValidationError("abandon_review_run_ids must not contain duplicates")
    if abandon_review_run_ids and command != WorkCommand.CANCEL:
        raise WorkItemValidationError("unknown review launches can only be abandoned during cancel")

    db_path = Path(db_path).expanduser()
    with closing(db_database.connect(db_path, isolation_level=None)) as conn, ExitStack() as locks:
        db_sqlite.configure(conn, foreign_keys=True)
        db_sqlite.configure(conn, busy_timeout_ms=5000)
        db_transactions.begin(conn, immediate=True)
        try:
            from robert_agent.persistence import execution_control as execution_store

            execution_store.require_migrated(conn)
            item = _load_item(conn, work_item_id)
            if item["repo_id"] not in context.allowed_repo_ids:
                raise WorkItemNotFoundError("work item not found")
            replay = _stored_command_result(conn, work_item_id, idempotency_key)
            if replay:
                return _commit_result(conn, db_path, replay, notifier)
            if item["version"] != expected_version:
                raise WorkItemConflictError("work item version is stale")

            if item["canceled_at"] is not None:
                raise WorkItemConflictError("a canceled work item cannot be changed")
            if item["completed_at"] is not None and command != WorkCommand.REOPEN:
                raise WorkItemConflictError("a completed work item must be reopened first")

            facts = control_queries.lifecycle_facts(conn, item)
            if command != WorkCommand.CANCEL and "result_contract_cutover" in facts["workflow_attention"]:
                raise WorkItemConflictError(
                    "Incompatible execution requires explicit recovery. Confirm workers stopped and use Retrigger for GitHub tasks, or Cancel and create replacement work."
                )
            if facts["source_closed"] and command != WorkCommand.CANCEL:
                raise WorkItemConflictError(
                    "The GitHub source is closed. Cancel this item; reopen the source on GitHub before starting new work."
                )
            audit_task = control_queries.current_task(conn, facts["streams"])
            audit_result_id = (
                _decode_json(audit_task["metadata_json"], {}).get("audit_retry_required") if audit_task else None
            )
            if command == WorkCommand.RETRY and audit_result_id:
                work_item_control.require_idle_repository(conn, item["repo_id"], utc_now())
                from robert_agent.delivery.lock import task_publication_lock

                if (
                    facts["running"]
                    or facts["workflow_attention"]
                    or facts["cleanup_pending"]
                    or not locks.enter_context(task_publication_lock(db_path, audit_task["task_id"]))
                ):
                    raise WorkItemConflictError(
                        "The saved result is not idle. Resolve active ownership before retrying its audit."
                    )
                saved = db_records.first(
                    db_read_worker_results.find_for_execute_command(conn, audit_result_id, audit_task["task_id"])
                )
                if not saved or _decode_json(saved["metadata_json"], {}).get("audit"):
                    raise WorkItemConflictError("The audit state changed. Refresh before retrying.")
                if db_records.first(db_read_publication_intents.find_for_execute_command(conn, audit_result_id)):
                    raise WorkItemConflictError("Publication has started; reconcile it before retrying.")
                now = utc_now()
                db_write_worker_results.update_for_execute_command(conn, audit_result_id)
                db_write_tasks.update_for_execute_command(conn, now, audit_task["task_id"])
                unresolved = _unresolved_event(conn, work_item_id, {"execution_failed"})
                if expected_event_id and (not unresolved or unresolved["event_id"] != expected_event_id):
                    raise WorkItemConflictError("The audit failure changed. Refresh before retrying.")
                db_write_work_items.record_github_response(conn, now, work_item_id)
                retry = {
                    "ok": True,
                    "command": command,
                    "item": _item_from_row(_load_item(conn, work_item_id)),
                    "task_id": audit_task["task_id"],
                    "result_id": audit_result_id,
                    "resolved_event_id": unresolved["event_id"] if unresolved else None,
                }
                retry_event = _insert_event(
                    conn,
                    work_item_id,
                    event_type="retry_requested",
                    actor_kind=context.actor_kind,
                    actor_identity=context.actor_identity,
                    idempotency_key=idempotency_key,
                    resolves_event_id=retry["resolved_event_id"],
                    metadata={"command_result": retry},
                    now=now,
                )
                supervision_lineage.record_operator_admission(conn, item, retry_event, audit_task["task_id"], now)
                return _commit_result(conn, db_path, retry, notifier)
            if facts["running"] and command not in {WorkCommand.CANCEL, WorkCommand.APPROVE}:
                raise WorkItemConflictError("An attempt is still running; stop it before starting another attempt.")
            if facts["actions"] and command not in {WorkCommand.CANCEL, WorkCommand.RETRY_PUBLISH}:
                raise WorkItemConflictError(
                    "A result is awaiting publication. Retry publication or cancel; do not start another worker."
                )
            now = utc_now()
            if command == WorkCommand.START:
                result = _activate(conn, item, context, idempotency_key, now)
                return _commit_result(conn, db_path, result, notifier)

            if command == WorkCommand.EDIT:
                if item["activated_at"] is not None or item["canceled_at"] is not None:
                    raise WorkItemConflictError("only a Backlog item can be edited")
                updates = {}
                if title is not None:
                    updates["title"] = validate_text(title, "title", MAX_TITLE, required=True)
                if description is not None:
                    updates["description"] = validate_text(
                        description,
                        "description",
                        MAX_DESCRIPTION,
                    )
                if priority is not None:
                    updates["priority"] = validate_priority(priority)
                if routing_mode is not None:
                    selected_mode, selected_worker = validate_routing(
                        context,
                        routing_mode,
                        requested_worker,
                    )
                    updates["routing_mode"] = selected_mode
                    updates["requested_worker"] = selected_worker
                elif requested_worker is not None:
                    raise WorkItemValidationError("routing_mode is required with requested_worker")
                if not updates:
                    raise WorkItemValidationError("edit requires at least one field")
                db_write_work_items.edit_fields(conn, work_item_id, updates, now)
                resolved_event_id = None
                task_id = None
            elif command == WorkCommand.CANCEL:
                work_item_control.cancel_execution(
                    conn, item, facts, now, db_path, locks, abandon_review_run_ids=abandon_review_run_ids
                )
                db_write_work_items.update_for_execute_command(conn, now, now, work_item_id)
                resolved_event_id = None
                task_id = None
            elif command == WorkCommand.RETRY_PUBLISH:
                if facts["publication_retry_blocked"]:
                    raise WorkItemConflictError(
                        "Publication reconciliation requires operator attention. Cancel the task and resolve the reported cause before starting new work."
                    )
                if not facts["publish_failures"]:
                    raise WorkItemConflictError("No failed publication is available to retry.")
                if facts["publication_retry_pending"]:
                    raise WorkItemConflictError("A publication retry is already queued.")
                failed_action_id = facts["publish_failures"][0]["action_id"]
                task_id = next(
                    action["task_id"] for action in facts["actions"] if action["action_id"] == failed_action_id
                )
                wakeup.request_wakeup(
                    conn,
                    repo_id=item["repo_id"],
                    reason="manual_operator_request",
                    dedupe_key=f"publication-retry:{idempotency_key}",
                    work_item_id=work_item_id,
                    task_id=task_id,
                    metadata={"kind": PUBLICATION_RETRY_WAKEUP_KIND},
                    now=now,
                )
                db_write_work_items.update_for_execute_command_2(conn, now, work_item_id)
                resolved_event_id = None
            elif (
                command == WorkCommand.RETRY
                and (handoff_failure := _unresolved_event(conn, work_item_id, {"execution_failed"}))
                and (_decode_json(handoff_failure["metadata_json"], {}).get("kind") == "pending_handoff")
            ):
                work_item_control.require_idle_repository(conn, item["repo_id"], now)
                parent = _active_or_latest_task(conn, item["workstream_id"])
                handoff = _decode_json(parent["metadata_json"], {}).get("pending_handoff", {}) if parent else {}
                from robert_agent.execution.ownership import subject_for_task

                binding = (
                    execution_store.get_binding(conn, *subject_for_task(conn, parent["task_id"])) if parent else None
                )
                stream = db_records.first(
                    db_read_workstreams.find_lifecycle_and_active_task_id_by_workstream_id(conn, item["workstream_id"])
                )
                if (
                    not parent
                    or parent["lifecycle"] != TaskLifecycle.COMPLETED
                    or handoff.get("status") != "attention"
                    or _decode_json(handoff_failure["metadata_json"], {}).get("task_id") != parent["task_id"]
                    or not stream
                    or stream[0] != WorkstreamLifecycle.ACTIVE.value
                    or stream[1] != parent["task_id"]
                    or not binding
                    or binding["binding_status"] != BindingStatus.ACTIVE
                    or (expected_event_id and handoff_failure["event_id"] != expected_event_id)
                ):
                    raise WorkItemConflictError("The pending request changed. Refresh before retrying.")
                db_write_tasks.update_for_execute_command_2(
                    conn,
                    json.dumps({"status": "pending", "retryable": True, "previous_failure": handoff}),
                    now,
                    parent["task_id"],
                )
                db_write_work_items.update_for_execute_command_2(conn, now, work_item_id)
                task_id = parent["task_id"]
                resolved_event_id = handoff_failure["event_id"]
            elif command == WorkCommand.REOPEN:
                if item["completed_at"] is None or item["canceled_at"] is not None:
                    raise WorkItemConflictError("only a completed work item can be reopened")
                db_write_work_items.update_for_execute_command_3(conn, now, work_item_id)
                parent = _active_or_latest_task(conn, item["workstream_id"])
                parent_task_id = _finish_parent_for_child(conn, parent, now)
                item = _load_item(conn, work_item_id)
                task_id = _create_task(
                    conn,
                    item,
                    parent_task_id=parent_task_id,
                    trigger_event_id=idempotency_key,
                    now=now,
                    lineage_key=supervision_lineage.operator_key(work_item_id, idempotency_key),
                )
                resolved_event_id = None
            else:
                allowed_events = QUESTION_EVENTS | FAILURE_EVENTS
                if command == WorkCommand.REPLY:
                    allowed_events = QUESTION_EVENTS
                elif command == WorkCommand.RETRY:
                    allowed_events = FAILURE_EVENTS
                elif command == WorkCommand.APPROVE:
                    allowed_events = DECISION_EVENTS | {"completion_acceptance_requested"}
                unresolved = _unresolved_event(conn, work_item_id, allowed_events)
                if expected_event_id is not None and (not unresolved or unresolved["event_id"] != expected_event_id):
                    raise WorkItemConflictError(
                        "The attention item changed. Review the latest question or failure before continuing."
                    )
                review_changes = (
                    command == WorkCommand.REQUEST_CHANGES
                    and not unresolved
                    and _has_open_pull_request(conn, item["workstream_id"])
                )
                parent = _active_or_latest_task(conn, item["workstream_id"])
                failed_retry = (
                    command == WorkCommand.RETRY
                    and parent
                    and parent["lifecycle"] == TaskLifecycle.FAILED
                    and item["origin_type"] == "web"
                )
                if not unresolved and not review_changes and not failed_retry:
                    raise WorkItemConflictError("work item is not waiting for operator input")
                if command == WorkCommand.REPLY and unresolved["event_type"] not in QUESTION_EVENTS:
                    raise WorkItemConflictError("the current attention item does not accept a reply")
                if command == WorkCommand.RETRY and unresolved and unresolved["event_type"] not in FAILURE_EVENTS:
                    raise WorkItemConflictError("the current attention item is not retryable")
                if command in {WorkCommand.REPLY, WorkCommand.REQUEST_CHANGES} and not body.strip():
                    raise WorkItemValidationError("body is required")
                if facts["running"] and (not unresolved or unresolved["event_type"] not in DECISION_EVENTS):
                    raise WorkItemConflictError("An attempt is still running.")
                unavailable_retry = (
                    command == WorkCommand.RETRY
                    and unresolved
                    and unresolved["event_type"] == "manual_worker_unavailable"
                    and parent
                    and parent["lifecycle"]
                    in {
                        TaskLifecycle.DETECTED,
                        TaskLifecycle.AUTHORIZED,
                        TaskLifecycle.CLASSIFIED,
                        TaskLifecycle.QUEUED,
                    }
                    and (_decode_json(unresolved["metadata_json"], {}) or {}).get("task_id") == parent["task_id"]
                )
                if unavailable_retry:
                    work_item_control.require_idle_repository(conn, item["repo_id"], now)
                    db_write_attempts.update_for_execute_command(conn, now, parent["task_id"])
                    db_write_tasks.update_for_dispatch_queued_locked_2(conn, now, parent["task_id"])
                if (
                    not unavailable_retry
                    and parent
                    and parent["lifecycle"] in ACTIVE_TASK_LIFECYCLES
                    and not (
                        command == WorkCommand.APPROVE and unresolved and unresolved["event_type"] in DECISION_EVENTS
                    )
                ):
                    raise WorkItemConflictError("An active task already exists. Wait for its result or cancel it.")
                unresolved_metadata = _decode_json(unresolved["metadata_json"], {}) or {} if unresolved else {}
                if command == WorkCommand.APPROVE and unresolved["event_type"] in DECISION_EVENTS:
                    if not facts["approval_task_id"]:
                        raise WorkItemConflictError(
                            "The route decision has no active task. Refresh and reconcile or cancel this item."
                        )
                    db_write_work_items.update_for_execute_command_4(conn, now, now, work_item_id)
                    task_id = None
                elif command == WorkCommand.APPROVE and unresolved_metadata.get("kind") == "completion_acceptance":
                    parent = _active_or_latest_task(conn, item["workstream_id"])
                    if parent and parent["lifecycle"] in {TaskLifecycle.WAITING_FOR_USER, TaskLifecycle.FAILED}:
                        db_write_tasks.update_for_finish_parent_for_child(conn, now, parent["task_id"])
                    db_write_workstreams.update_for_execute_command(conn, now, item["workstream_id"])
                    db_write_work_items.update_for_execute_command_5(conn, now, now, work_item_id)
                    task_id = None
                else:
                    selected_mode = item["routing_mode"]
                    selected_worker = item["requested_worker"]
                    if routing_mode is not None:
                        selected_mode, selected_worker = validate_routing(
                            context,
                            routing_mode,
                            requested_worker,
                        )
                        db_write_work_items.set_routing_mode_and_requested_worker_by_work_item_id(
                            conn, selected_mode, selected_worker, work_item_id
                        )
                    elif requested_worker is not None:
                        raise WorkItemValidationError("routing_mode is required with requested_worker")
                    parent = _active_or_latest_task(conn, item["workstream_id"])
                    question_task_id = unresolved_metadata.get("task_id") if command == WorkCommand.REPLY else None
                    if question_task_id:
                        question_task = db_records.first(db_read_tasks.find_record_by_task_id(conn, question_task_id))
                        if (
                            not question_task
                            or question_task["workstream_id"] not in facts["streams"]
                            or question_task["lifecycle"] != TaskLifecycle.WAITING_FOR_USER
                        ):
                            raise WorkItemConflictError("The question owner changed. Refresh before replying.")
                        parent = question_task
                    parent_task_id = _finish_parent_for_child(conn, parent, now)
                    db_write_work_items.update_for_execute_command_2(conn, now, work_item_id)
                    item = _load_item(conn, work_item_id)
                    task_id = _create_task(
                        conn,
                        item,
                        parent_task_id=parent_task_id,
                        routing_mode=selected_mode,
                        requested_worker=selected_worker,
                        trigger_event_id=idempotency_key,
                        now=now,
                        workstream_id=parent["workstream_id"] if question_task_id else None,
                        lineage_key=supervision_lineage.get_task_lineage(conn, parent_task_id),
                    )
                resolved_event_id = unresolved["event_id"] if unresolved else None

            current_item = _item_from_row(_load_item(conn, work_item_id))
            result = {
                "ok": True,
                "command": command,
                "item": current_item,
                "task_id": task_id,
                "resolved_event_id": resolved_event_id,
            }
            if abandon_review_run_ids:
                result["abandoned_review_run_ids"] = abandon_review_run_ids
            command_event = _insert_event(
                conn,
                work_item_id,
                event_type=_command_event_type(command),
                actor_kind=context.actor_kind,
                actor_identity=context.actor_identity,
                idempotency_key=idempotency_key,
                body=body,
                resolves_event_id=resolved_event_id,
                metadata={"command_result": result, "task_id": task_id},
                now=now,
            )
            supervision_lineage.record_operator_admission(conn, item, command_event, task_id, now)
            return _commit_result(conn, db_path, result, notifier)
        except Exception:
            conn.rollback()
            raise
