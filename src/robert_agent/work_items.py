"""Transactional commands for repository-scoped DD work items."""

from contextlib import closing, ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from uuid import uuid4

from robert_agent import wakeup, wakeup_notifier, work_item_control


MAX_TITLE = 200
MAX_DESCRIPTION = 20_000
MAX_REPLY = 8_000
MAX_IDEMPOTENCY_KEY = 128
PRIORITIES = frozenset({"P0", "P1", "P2", "P3"})
COMMANDS = frozenset(
    {
        "edit",
        "start",
        "approve",
        "reply",
        "request_changes",
        "retry",
        "retry_publish",
        "cancel",
        "reopen",
    }
)
QUESTION_EVENTS = frozenset({"operator_question", "completion_acceptance_requested"})
DECISION_EVENTS = frozenset({"operator_decision_required"})
FAILURE_EVENTS = frozenset(
    {
        "execution_failed",
        "publication_failed",
        "manual_worker_unavailable",
        "review_attention",
        "unmerged_pr_closed",
    }
)


@dataclass(frozen=True)
class CommandContext:
    actor_kind: str
    actor_identity: str
    allowed_repo_ids: frozenset[str]
    allowed_workers: frozenset[str]


class WorkItemValidationError(ValueError):
    pass


class WorkItemConflictError(RuntimeError):
    pass


class WorkItemNotFoundError(LookupError):
    pass


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


def _validate_idempotency_key(value):
    if not isinstance(value, str) or not value.strip():
        raise WorkItemValidationError("idempotency_key is required")
    if len(value) > MAX_IDEMPOTENCY_KEY:
        raise WorkItemValidationError("idempotency_key is too long")
    return value


def _validate_text(value, field, maximum, *, required=False):
    if not isinstance(value, str):
        raise WorkItemValidationError(f"{field} must be a string")
    value = value.strip() if required else value
    if required and not value:
        raise WorkItemValidationError(f"{field} is required")
    if len(value) > maximum:
        raise WorkItemValidationError(f"{field} is too long")
    return value


def _validate_priority(priority):
    if priority not in PRIORITIES:
        raise WorkItemValidationError("priority must be one of P0, P1, P2, P3")
    return priority


def _validate_routing(context, routing_mode, requested_worker):
    if routing_mode not in {"auto", "manual"}:
        raise WorkItemValidationError("routing_mode must be auto or manual")
    if routing_mode == "auto":
        if requested_worker is not None:
            raise WorkItemValidationError("automatic routing cannot select a worker")
        return routing_mode, None
    requested_worker = _validate_text(
        requested_worker,
        "requested_worker",
        MAX_TITLE,
        required=True,
    )
    if requested_worker not in context.allowed_workers:
        raise WorkItemValidationError("requested worker is not available")
    return routing_mode, requested_worker


def _item_from_row(row):
    if not hasattr(row, "keys"):
        columns = (
            "work_item_id", "repo_id", "title", "description", "priority",
            "origin_type", "origin_source_id", "routing_mode", "requested_worker",
            "workstream_id", "creation_idempotency_key", "created_by", "activated_at",
            "completed_at", "canceled_at", "version", "created_at", "updated_at",
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
    row = conn.execute(
        "SELECT * FROM work_items WHERE work_item_id = ?",
        (work_item_id,),
    ).fetchone()
    if not row:
        raise WorkItemNotFoundError("work item not found")
    return row


def _event_from_row(row):
    if not hasattr(row, "keys"):
        columns = (
            "event_id", "work_item_id", "event_type", "actor_kind",
            "actor_identity", "body", "resolves_event_id", "idempotency_key",
            "created_at", "metadata_json",
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
    conn.execute(
        """
        INSERT INTO work_item_events(
          event_id, work_item_id, event_type, actor_kind, actor_identity,
          body, resolves_event_id, idempotency_key, created_at, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
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
        ),
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
    _validate_idempotency_key(idempotency_key)
    _load_item(conn, work_item_id)
    existing = conn.execute(
        """
        SELECT * FROM work_item_events
        WHERE work_item_id = ? AND idempotency_key = ?
        """,
        (work_item_id, idempotency_key),
    ).fetchone()
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
        row = conn.execute(
            "SELECT * FROM work_items WHERE workstream_id = ?",
            (current,),
        ).fetchone()
        if row:
            return _item_from_row(row)
        stream = conn.execute(
            "SELECT origin_workstream_id FROM workstreams WHERE workstream_id = ?",
            (current,),
        ).fetchone()
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
        row = conn.execute(
            """
            SELECT origin_workstream_id
            FROM workstreams
            WHERE workstream_id = ?
            """,
            (root_workstream_id,),
        ).fetchone()
        if not row or not row[0]:
            break
        root_workstream_id = row[0]

    root = conn.execute(
        """
        SELECT w.primary_source_id, w.created_at, gs.title, gs.author_login
        FROM workstreams w
        LEFT JOIN github_sources gs ON gs.source_id = w.primary_source_id
        WHERE w.workstream_id = ?
        """,
        (root_workstream_id,),
    ).fetchone()
    root_source_id = (root[0] if root else None) or source_id
    duplicate = conn.execute(
        "SELECT * FROM work_items WHERE origin_source_id = ?",
        (root_source_id,),
    ).fetchone()
    if duplicate:
        return _item_from_row(duplicate)

    digest = hashlib.sha256(root_workstream_id.encode("utf-8")).hexdigest()[:20]
    work_item_id = f"wi-{digest}"
    title = (root[2] if root else None) or f"Workstream {root_workstream_id}"
    created_by = actor_identity or (root[3] if root else None) or "system"
    created_at = (root[1] if root else None) or now
    activated_at = now if route_confidence == "high" else None
    conn.execute(
        """
        INSERT INTO work_items(
          work_item_id, repo_id, title, description, priority,
          origin_type, origin_source_id, routing_mode, requested_worker,
          workstream_id, creation_idempotency_key, created_by, activated_at,
          version, created_at, updated_at, metadata_json
        )
        VALUES (?, ?, ?, '', 'P1', 'github', ?, 'auto', NULL, ?, ?, ?, ?, 1, ?, ?, '{}')
        ON CONFLICT DO NOTHING
        """,
        (
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
        ),
    )
    item = conn.execute(
        "SELECT * FROM work_items WHERE workstream_id = ? OR origin_source_id = ?",
        (root_workstream_id, root_source_id),
    ).fetchone()
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
    row = conn.execute(
        """
        SELECT metadata_json
        FROM work_item_events
        WHERE work_item_id = ? AND idempotency_key = ?
        """,
        (work_item_id, idempotency_key),
    ).fetchone()
    if not row:
        return None
    metadata = _decode_json(row[0], {}) or {}
    return metadata.get("command_result")


def _unresolved_event(conn, work_item_id, event_types):
    placeholders = ",".join("?" for _event_type in event_types)
    return conn.execute(
        f"""
        SELECT e.*
        FROM work_item_events e
        WHERE e.work_item_id = ?
          AND e.event_type IN ({placeholders})
          AND NOT EXISTS (
            SELECT 1 FROM work_item_events response
            WHERE response.resolves_event_id = e.event_id
          )
        ORDER BY e.created_at DESC, e.event_id DESC
        LIMIT 1
        """,
        [work_item_id] + sorted(event_types),
    ).fetchone()


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
    idempotency_key = (
        "github-response:"
        + hashlib.sha256(event_fingerprint.encode("utf-8")).hexdigest()[:24]
    )
    existing = conn.execute(
        "SELECT * FROM work_item_events WHERE work_item_id = ? AND idempotency_key = ?",
        (item["work_item_id"], idempotency_key),
    ).fetchone()
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
    conn.execute(
        "UPDATE work_items SET version = version + 1, updated_at = ? WHERE work_item_id = ?",
        (now, item["work_item_id"]),
    )
    return response


def _has_open_pull_request(conn, workstream_id):
    if not workstream_id:
        return False
    return bool(
        conn.execute(
            """
            SELECT 1
            FROM workstreams w
            JOIN github_sources gs ON gs.source_id = w.primary_source_id
            WHERE (w.workstream_id = ? OR w.origin_workstream_id = ?)
              AND gs.source_type = 'pull_request'
              AND LOWER(COALESCE(gs.state, 'open')) = 'open'
            LIMIT 1
            """,
            (workstream_id, workstream_id),
        ).fetchone()
    )


def _active_or_latest_task(conn, workstream_id):
    row = conn.execute(
        """
        SELECT t.*
        FROM workstreams w
        JOIN tasks t ON t.task_id = w.active_task_id
        WHERE w.workstream_id = ?
        """,
        (workstream_id,),
    ).fetchone()
    if row:
        return row
    return conn.execute(
        """
        SELECT * FROM tasks
        WHERE workstream_id = ?
        ORDER BY created_at DESC, task_id DESC
        LIMIT 1
        """,
        (workstream_id,),
    ).fetchone()


def _create_task(
    conn,
    item,
    *,
    parent_task_id=None,
    routing_mode=None,
    requested_worker=None,
    trigger_event_id=None,
    now,
):
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
    if trigger_event_id:
        metadata["trigger_work_item_event_id"] = trigger_event_id
    conn.execute(
        """
        INSERT INTO tasks(
          task_id, workstream_id, lifecycle, parent_task_id, priority,
          routing_mode, requested_worker, created_at, updated_at, metadata_json
        ) VALUES (?, ?, 'detected', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            item["workstream_id"],
            parent_task_id,
            item["priority"],
            routing_mode,
            requested_worker,
            now,
            now,
            json.dumps(metadata, sort_keys=True),
        ),
    )
    conn.execute(
        """
        UPDATE workstreams
        SET lifecycle = 'active', active_task_id = ?, updated_at = ?
        WHERE workstream_id = ?
        """,
        (task_id, now, item["workstream_id"]),
    )
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
    idempotency_key = _validate_idempotency_key(idempotency_key)
    db_path = Path(db_path).expanduser()
    # Refresh missing source content outside the write transaction. Preserve
    # the original event; the new prompt receives an audited context snapshot.
    refreshed_context = None
    recovery_snapshot = None
    with closing(sqlite3.connect(db_path)) as reader:
        reader.row_factory = sqlite3.Row
        snapshot = reader.execute("""SELECT t.metadata_json, r.full_name, r.github_account, w.repo_id
            FROM tasks t JOIN workstreams w ON w.workstream_id=t.workstream_id
            JOIN repos r ON r.repo_id=w.repo_id WHERE t.task_id=?""", (task_id,)).fetchone()
        if snapshot and snapshot['repo_id'] in context.allowed_repo_ids:
            attention = _decode_json(snapshot['metadata_json'], {}).get('execution_attention', {})
            if attention.get('reason_code') == 'result_contract_cutover':
                recovery_snapshot = snapshot['metadata_json']
                from robert_agent import discover
                from robert_agent.run_once import _task_related_events
                refreshed_context = _task_related_events(reader, task_id)
                refreshed = []
                for event in refreshed_context:
                    updated, state = discover._source_metadata(event, dict(snapshot), subprocess.run)
                    if state is None:
                        raise WorkItemConflictError('GitHub source refresh failed; retry after access is restored')
                    if state.lower() != 'open':
                        raise WorkItemConflictError('retrigger requires an open GitHub source')
                    refreshed.append(updated)
                if not refreshed:
                    raise WorkItemConflictError('source context is unavailable; inspect the original task')
                refreshed_context = refreshed
    from robert_agent.publication_lock import task_publication_lock
    with ExitStack() as locks, closing(sqlite3.connect(db_path, isolation_level=None)) as conn:
        if not locks.enter_context(task_publication_lock(db_path, task_id)):
            raise WorkItemConflictError('A publication is in progress; retry after it finishes')
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """
                SELECT t.task_id, t.lifecycle, t.workstream_id, w.repo_id,
                       w.lifecycle AS workstream_lifecycle, w.active_task_id,
                       gs.state AS source_state, t.metadata_json
                FROM tasks t
                JOIN workstreams w ON w.workstream_id = t.workstream_id
                JOIN github_sources gs ON gs.source_id = w.primary_source_id
                WHERE t.task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if not row or row["repo_id"] not in context.allowed_repo_ids:
                raise WorkItemNotFoundError("task not found")
            from robert_agent import execution_store
            binding = execution_store.get_binding(conn, "task", task_id)
            attention = _decode_json(row['metadata_json'], {}).get('execution_attention', {})
            cutover_recovery = attention.get('reason_code') == 'result_contract_cutover'
            if cutover_recovery and recovery_snapshot != row['metadata_json']:
                raise WorkItemConflictError('execution state changed; refresh before recovery')
            if row["lifecycle"] != "failed" and not cutover_recovery:
                raise WorkItemConflictError("only a failed GitHub task can be retriggered")
            if row["source_state"].lower() != "open":
                raise WorkItemConflictError("retrigger requires an open GitHub source")
            if row["active_task_id"] is not None and not (cutover_recovery and row['active_task_id'] == task_id):
                raise WorkItemConflictError("the workstream already has an active task")
            latest = conn.execute(
                """
                SELECT task_id FROM tasks
                WHERE workstream_id = ?
                ORDER BY created_at DESC, task_id DESC
                LIMIT 1
                """,
                (row["workstream_id"],),
            ).fetchone()
            if not latest or latest[0] != task_id:
                raise WorkItemConflictError("only the latest failed GitHub task can be retriggered")

            # Explicit recovery creates a new subject; never reopen or transfer
            # ownership of the old workflow, nor overlap an uncertain process.
            if conn.execute("SELECT 1 FROM attempts WHERE task_id=? AND status IN ('prepared','running','stale')", (task_id,)).fetchone():
                raise WorkItemConflictError("worker termination must be confirmed before retry")
            if conn.execute("SELECT 1 FROM github_actions WHERE task_id=? AND audit_status IN ('pending','accepted') AND publish_status='not_published'", (task_id,)).fetchone():
                raise WorkItemConflictError("pending publication must be reconciled or canceled before retry")
            failed_attempt = conn.execute("SELECT metadata_json FROM attempts WHERE task_id=? ORDER BY attempt_no DESC LIMIT 1", (task_id,)).fetchone()
            if failed_attempt:
                process_metadata = json.loads(failed_attempt[0] or '{}')
                if process_metadata.get('dispatch'):
                    from robert_agent.run_once import _dispatch_process_status
                    if _dispatch_process_status(process_metadata)['status'] != 'dead':
                        raise WorkItemConflictError("worker termination must be confirmed before retry")
            if cutover_recovery:
                from robert_agent import native_rounds
                _, owner = native_rounds.owner(conn, task_id)
                if owner:
                    raise WorkItemConflictError('Review workers must be reconciled before recovery')
                recovery_now = utc_now()
                work_item_control.require_idle_repository(conn, row['repo_id'], recovery_now)
                metadata = _decode_json(row['metadata_json'], {})
                metadata['recovered_execution_attention'] = {**metadata.pop('execution_attention'),
                    'recovered_at': recovery_now, 'requested_by': context.actor_identity}
                conn.execute("UPDATE tasks SET lifecycle='failed',metadata_json=?,updated_at=? WHERE task_id=?",
                             (json.dumps(metadata), recovery_now, task_id))
                conn.execute("UPDATE workstreams SET lifecycle='failed',active_task_id=NULL,updated_at=? WHERE workstream_id=?",
                             (recovery_now, row['workstream_id']))
                item = resolve_work_item_for_workstream(conn, row['workstream_id'])
                if item:
                    unresolved = conn.execute("SELECT event_id FROM work_item_events WHERE work_item_id=? AND idempotency_key=?",
                                              (item['work_item_id'], 'result-contract-cutover:' + task_id)).fetchone()
                    _insert_event(conn, item['work_item_id'], event_type='execution_recovery_requested',
                        actor_kind=context.actor_kind, actor_identity=context.actor_identity,
                        idempotency_key='cutover-recovery:' + hashlib.sha256(idempotency_key.encode()).hexdigest(),
                        resolves_event_id=unresolved['event_id'] if unresolved else None,
                        body='Replace incompatible execution using refreshed GitHub context; preserve original evidence.',
                        metadata={'task_id': task_id, 'kind': 'result_contract_cutover'}, now=recovery_now)
                conn.execute("UPDATE notifications SET status='resolved' WHERE notification_id=?", ('notification-cutover-' + task_id,))
            if binding and binding['binding_status'] != 'terminal':
                execution_store.close_binding(conn, "task", task_id, utc_now())

            dedupe_key = f"github-retrigger:{idempotency_key}"
            existing = conn.execute(
                """
                SELECT dedupe_key
                FROM wakeups
                WHERE repo_id = ?
                  AND reason = 'manual_operator_request'
                  AND task_id = ?
                  AND status = 'pending'
                  AND json_extract(metadata_json, '$.kind') = 'github_retrigger'
                """,
                (row["repo_id"], task_id),
            ).fetchone()
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
                    **({'refreshed_context': refreshed_context} if refreshed_context is not None else {}),
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
    conn.execute(
        """
        INSERT INTO workstreams(
          workstream_id, repo_id, lifecycle, active_task_id,
          created_at, updated_at, metadata_json
        ) VALUES (?, ?, 'active', NULL, ?, ?, ?)
        """,
        (
            workstream_id,
            item["repo_id"],
            now,
            now,
            json.dumps({"origin_type": "web", "work_item_id": item["work_item_id"]}),
        ),
    )
    conn.execute(
        """
        UPDATE work_items
        SET workstream_id = ?, activated_at = ?, updated_at = ?, version = ?
        WHERE work_item_id = ?
        """,
        (workstream_id, now, now, version, item["work_item_id"]),
    )
    item = _load_item(conn, item["work_item_id"])
    task_id = _create_task(conn, item, now=now)
    result = {"ok": True, "command": "start", "item": _item_from_row(_load_item(conn, item["work_item_id"])), "task_id": task_id}
    _insert_event(
        conn,
        item["work_item_id"],
        event_type="activated",
        actor_kind=context.actor_kind,
        actor_identity=context.actor_identity,
        idempotency_key=idempotency_key,
        metadata={"command_result": result, "task_id": task_id},
        now=now,
    )
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
    _validate_idempotency_key(idempotency_key)
    if repo_id not in context.allowed_repo_ids:
        raise WorkItemValidationError("repository is not enabled")
    title = _validate_text(title, "title", MAX_TITLE, required=True)
    description = _validate_text(description, "description", MAX_DESCRIPTION)
    priority = _validate_priority(priority)
    routing_mode, requested_worker = _validate_routing(
        context,
        routing_mode,
        requested_worker,
    )
    if not isinstance(start, bool):
        raise WorkItemValidationError("start must be a boolean")

    db_path = Path(db_path).expanduser()
    with closing(sqlite3.connect(db_path, isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT * FROM work_items WHERE creation_idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                metadata = _decode_json(existing["metadata_json"], {}) or {}
                result = metadata.get("command_result")
                if result:
                    return _commit_result(conn, db_path, result, notifier)
                result = {"ok": True, "command": "create", "item": _item_from_row(existing)}
                return _commit_result(conn, db_path, result, notifier)

            now = utc_now()
            work_item_id = f"wi-{uuid4().hex}"
            conn.execute(
                """
                INSERT INTO work_items(
                  work_item_id, repo_id, title, description, priority,
                  origin_type, routing_mode, requested_worker,
                  creation_idempotency_key, created_by, version,
                  created_at, updated_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, 'web', ?, ?, ?, ?, 1, ?, ?, '{}')
                """,
                (
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
                ),
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
                result = _activate(
                    conn,
                    item,
                    context,
                    f"start:{idempotency_key}",
                    now,
                    initial_version=1,
                )
                result["command"] = "create_and_start"
            else:
                result = {"ok": True, "command": "create", "item": _item_from_row(item)}
            conn.execute(
                "UPDATE work_items SET metadata_json = ? WHERE work_item_id = ?",
                (
                    json.dumps({"command_result": result}, ensure_ascii=False, sort_keys=True),
                    work_item_id,
                ),
            )
            return _commit_result(conn, db_path, result, notifier)
        except Exception:
            conn.rollback()
            raise


def _finish_parent_for_child(conn, parent_task, now):
    if not parent_task:
        return None
    if parent_task["lifecycle"] in {"waiting_for_user", "failed"}:
        conn.execute(
            "UPDATE tasks SET lifecycle = 'completed', updated_at = ? WHERE task_id = ?",
            (now, parent_task["task_id"]),
        )
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
    _validate_idempotency_key(idempotency_key)
    if command not in COMMANDS:
        raise WorkItemValidationError("unknown work item command")
    if not isinstance(expected_version, int) or isinstance(expected_version, bool):
        raise WorkItemValidationError("expected_version must be an integer")
    if expected_event_id is not None:
        expected_event_id = _validate_text(expected_event_id, "expected_event_id", 128, required=True)
    body = _validate_text(body, "body", MAX_REPLY)
    if abandon_review_run_ids is None:
        abandon_review_run_ids = []
    if not isinstance(abandon_review_run_ids, list):
        raise WorkItemValidationError('abandon_review_run_ids must be a list of run IDs')
    abandon_review_run_ids = [_validate_text(value, 'review run ID', 128, required=True)
                             for value in abandon_review_run_ids]
    if len(set(abandon_review_run_ids)) != len(abandon_review_run_ids):
        raise WorkItemValidationError('abandon_review_run_ids must not contain duplicates')
    if abandon_review_run_ids and command != 'cancel':
        raise WorkItemValidationError('unknown review launches can only be abandoned during cancel')

    db_path = Path(db_path).expanduser()
    with closing(sqlite3.connect(db_path, isolation_level=None)) as conn, ExitStack() as locks:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            from robert_agent import execution_store
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
            if item["completed_at"] is not None and command != "reopen":
                raise WorkItemConflictError("a completed work item must be reopened first")

            facts = work_item_control.lifecycle_facts(conn, item)
            if command != 'cancel' and 'result_contract_cutover' in facts['workflow_attention']:
                raise WorkItemConflictError('Incompatible execution requires explicit recovery. Confirm workers stopped and use Retrigger for GitHub tasks, or Cancel and create replacement work.')
            if facts["source_closed"] and command != "cancel":
                raise WorkItemConflictError("The GitHub source is closed. Cancel this item; reopen the source on GitHub before starting new work.")
            if facts["running"] and command not in {"cancel", "approve"}:
                raise WorkItemConflictError("An attempt is still running; stop it before starting another attempt.")
            if facts["actions"] and command not in {"cancel", "retry_publish"}:
                raise WorkItemConflictError("A result is awaiting publication. Retry publication or cancel; do not start another worker.")
            now = utc_now()
            if command == "start":
                result = _activate(conn, item, context, idempotency_key, now)
                return _commit_result(conn, db_path, result, notifier)

            if command == "edit":
                if item["activated_at"] is not None or item["canceled_at"] is not None:
                    raise WorkItemConflictError("only a Backlog item can be edited")
                updates = {}
                if title is not None:
                    updates["title"] = _validate_text(title, "title", MAX_TITLE, required=True)
                if description is not None:
                    updates["description"] = _validate_text(
                        description,
                        "description",
                        MAX_DESCRIPTION,
                    )
                if priority is not None:
                    updates["priority"] = _validate_priority(priority)
                if routing_mode is not None:
                    selected_mode, selected_worker = _validate_routing(
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
                assignments = ", ".join(f"{field} = ?" for field in updates)
                conn.execute(
                    f"""
                    UPDATE work_items
                    SET {assignments}, version = version + 1, updated_at = ?
                    WHERE work_item_id = ?
                    """,
                    list(updates.values()) + [now, work_item_id],
                )
                resolved_event_id = None
                task_id = None
            elif command == "cancel":
                work_item_control.cancel_execution(conn, item, facts, now, db_path, locks,
                                                   abandon_review_run_ids=abandon_review_run_ids)
                conn.execute(
                    """
                    UPDATE work_items
                    SET canceled_at = ?, updated_at = ?, version = version + 1
                    WHERE work_item_id = ?
                    """,
                    (now, now, work_item_id),
                )
                resolved_event_id = None
                task_id = None
            elif command == "retry_publish":
                if facts["publication_retry_blocked"]:
                    raise WorkItemConflictError("Publication reconciliation requires operator attention. Cancel the task and resolve the reported cause before starting new work.")
                if not facts["publish_failures"]:
                    raise WorkItemConflictError("No failed publication is available to retry.")
                if facts["publication_retry_pending"]:
                    raise WorkItemConflictError("A publication retry is already queued.")
                failed_action_id = facts["publish_failures"][0]["action_id"]
                task_id = next(action["task_id"] for action in facts["actions"] if action["action_id"] == failed_action_id)
                wakeup.request_wakeup(conn, repo_id=item["repo_id"], reason="manual_operator_request",
                    dedupe_key=f"publication-retry:{idempotency_key}", work_item_id=work_item_id,
                    task_id=task_id, metadata={"kind": "publication_retry"}, now=now)
                conn.execute("UPDATE work_items SET updated_at=?, version=version+1 WHERE work_item_id=?", (now, work_item_id))
                resolved_event_id = None
            elif command == "reopen":
                if item["completed_at"] is None or item["canceled_at"] is not None:
                    raise WorkItemConflictError("only a completed work item can be reopened")
                conn.execute(
                    "UPDATE work_items SET completed_at = NULL, updated_at = ?, version = version + 1 WHERE work_item_id = ?",
                    (now, work_item_id),
                )
                parent = _active_or_latest_task(conn, item["workstream_id"])
                parent_task_id = _finish_parent_for_child(conn, parent, now)
                item = _load_item(conn, work_item_id)
                task_id = _create_task(
                    conn,
                    item,
                    parent_task_id=parent_task_id,
                    trigger_event_id=idempotency_key,
                    now=now,
                )
                resolved_event_id = None
            else:
                allowed_events = QUESTION_EVENTS | FAILURE_EVENTS
                if command == "reply":
                    allowed_events = QUESTION_EVENTS
                elif command == "retry":
                    allowed_events = FAILURE_EVENTS
                elif command == "approve":
                    allowed_events = DECISION_EVENTS | {"completion_acceptance_requested"}
                unresolved = _unresolved_event(conn, work_item_id, allowed_events)
                if expected_event_id is not None and (not unresolved or unresolved["event_id"] != expected_event_id):
                    raise WorkItemConflictError("The attention item changed. Review the latest question or failure before continuing.")
                review_changes = (
                    command == "request_changes"
                    and not unresolved
                    and _has_open_pull_request(conn, item["workstream_id"])
                )
                parent = _active_or_latest_task(conn, item["workstream_id"])
                failed_retry = command == "retry" and parent and parent["lifecycle"] == "failed" and item["origin_type"] == "web"
                if not unresolved and not review_changes and not failed_retry:
                    raise WorkItemConflictError("work item is not waiting for operator input")
                if command == "reply" and unresolved["event_type"] not in QUESTION_EVENTS:
                    raise WorkItemConflictError("the current attention item does not accept a reply")
                if command == "retry" and unresolved and unresolved["event_type"] not in FAILURE_EVENTS:
                    raise WorkItemConflictError("the current attention item is not retryable")
                if command in {"reply", "request_changes"} and not body.strip():
                    raise WorkItemValidationError("body is required")
                if facts["running"] and (not unresolved or unresolved["event_type"] not in DECISION_EVENTS):
                    raise WorkItemConflictError("An attempt is still running.")
                unavailable_retry = (
                    command == "retry" and unresolved and unresolved["event_type"] == "manual_worker_unavailable"
                    and parent and parent["lifecycle"] in {"detected", "authorized", "classified", "queued"}
                    and (_decode_json(unresolved["metadata_json"], {}) or {}).get("task_id") == parent["task_id"]
                )
                if unavailable_retry:
                    work_item_control.require_idle_repository(conn, item["repo_id"], now)
                    conn.execute("UPDATE attempts SET status='canceled',finished_at=? WHERE task_id=? AND status='prepared'", (now, parent["task_id"]))
                    conn.execute("UPDATE tasks SET lifecycle='canceled',updated_at=? WHERE task_id=?", (now, parent["task_id"]))
                if not unavailable_retry and parent and parent["lifecycle"] in {"detected", "authorized", "classified", "queued", "running"} and not (command == "approve" and unresolved and unresolved["event_type"] in DECISION_EVENTS):
                    raise WorkItemConflictError("An active task already exists. Wait for its result or cancel it.")
                unresolved_metadata = (
                    _decode_json(unresolved["metadata_json"], {}) or {}
                    if unresolved
                    else {}
                )
                if command == "approve" and unresolved["event_type"] in DECISION_EVENTS:
                    if not facts["approval_task_id"]:
                        raise WorkItemConflictError("The route decision has no active task. Refresh and reconcile or cancel this item.")
                    conn.execute(
                        """
                        UPDATE work_items
                        SET activated_at = COALESCE(activated_at, ?),
                            updated_at = ?,
                            version = version + 1
                        WHERE work_item_id = ?
                        """,
                        (now, now, work_item_id),
                    )
                    task_id = None
                elif command == "approve" and unresolved_metadata.get("kind") == "completion_acceptance":
                    parent = _active_or_latest_task(conn, item["workstream_id"])
                    if parent and parent["lifecycle"] in {"waiting_for_user", "failed"}:
                        conn.execute(
                            "UPDATE tasks SET lifecycle = 'completed', updated_at = ? WHERE task_id = ?",
                            (now, parent["task_id"]),
                        )
                    conn.execute(
                        "UPDATE workstreams SET lifecycle = 'completed', active_task_id = NULL, updated_at = ? WHERE workstream_id = ?",
                        (now, item["workstream_id"]),
                    )
                    conn.execute(
                        "UPDATE work_items SET completed_at = ?, updated_at = ?, version = version + 1 WHERE work_item_id = ?",
                        (now, now, work_item_id),
                    )
                    task_id = None
                else:
                    selected_mode = item["routing_mode"]
                    selected_worker = item["requested_worker"]
                    if routing_mode is not None:
                        selected_mode, selected_worker = _validate_routing(
                            context,
                            routing_mode,
                            requested_worker,
                        )
                        conn.execute(
                            "UPDATE work_items SET routing_mode = ?, requested_worker = ? WHERE work_item_id = ?",
                            (selected_mode, selected_worker, work_item_id),
                        )
                    elif requested_worker is not None:
                        raise WorkItemValidationError("routing_mode is required with requested_worker")
                    parent = _active_or_latest_task(conn, item["workstream_id"])
                    parent_task_id = _finish_parent_for_child(conn, parent, now)
                    conn.execute(
                        "UPDATE work_items SET updated_at = ?, version = version + 1 WHERE work_item_id = ?",
                        (now, work_item_id),
                    )
                    item = _load_item(conn, work_item_id)
                    task_id = _create_task(
                        conn,
                        item,
                        parent_task_id=parent_task_id,
                        routing_mode=selected_mode,
                        requested_worker=selected_worker,
                        trigger_event_id=idempotency_key,
                        now=now,
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
                result['abandoned_review_run_ids'] = abandon_review_run_ids
            _insert_event(
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
            return _commit_result(conn, db_path, result, notifier)
        except Exception:
            conn.rollback()
            raise
