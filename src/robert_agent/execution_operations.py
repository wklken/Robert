"""SQLite subject state, process identity and domain recovery operations."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from robert_agent import worker_identity, retry_policy
from robert_agent import execution_store

_TERMINAL_TASK_LIFECYCLES = {"completed", "failed", "canceled", "ignored"}
_TERMINAL_EPISODE_STATUSES = {"resolved", "exhausted", "superseded", "canceled"}
_SENSITIVE_METADATA_KEYS = {
    "api_key",
    "authorization",
    "credential",
    "credentials",
    "database_url",
    "password",
    "private_key",
    "secret",
    "system_database_url",
    "token",
}


def _json_object(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_metadata(value):
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.lower().replace("-", "_")
            if normalized in _SENSITIVE_METADATA_KEYS or any(
                token in normalized
                for token in (
                    "credential",
                    "password",
                    "secret",
                    "token",
                    "private_key",
                )
            ):
                continue
            safe[key_text] = _safe_metadata(item)
        return safe
    if isinstance(value, list):
        return [_safe_metadata(item) for item in value]
    return value


def _safe_row(row):
    if row is None:
        return None
    result = {}
    for key in row.keys():
        if key == "metadata_json":
            result["metadata"] = _safe_metadata(_json_object(row[key]))
            continue
        result[key] = row[key]
    return result


def _safe_binding(binding):
    if not binding:
        return binding
    safe = dict(binding)
    metadata_json = safe.pop("metadata_json", None)
    safe["metadata"] = _safe_metadata(_json_object(metadata_json))
    return safe


def _row(conn, query, params=()):
    return conn.execute(query, params).fetchone()


def _validate_subject_type(subject_type):
    if subject_type not in {"task", "remediation_episode"}:
        raise ValueError("invalid workflow subject type")


def _load_task(conn, task_id):
    row = _row(conn, "SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    if row is None:
        raise ValueError("workflow task subject does not exist")
    return row


def _load_episode(conn, episode_id):
    row = _row(
        conn,
        "SELECT * FROM pr_remediation_episodes WHERE episode_id = ?",
        (episode_id,),
    )
    if row is None:
        raise ValueError("workflow remediation episode subject does not exist")
    return row


def _task_relations(conn, task_id):
    attempts = conn.execute(
        """
        SELECT attempt_id, attempt_no, status, worktree_path, branch_name,
               started_at, heartbeat_at, finished_at
        FROM attempts
        WHERE task_id = ?
        ORDER BY attempt_no, attempt_id
        """,
        (task_id,),
    ).fetchall()
    results = conn.execute(
        """
        SELECT result_id, attempt_id, output_type, created_at
        FROM worker_results
        WHERE task_id = ?
        ORDER BY created_at, result_id
        """,
        (task_id,),
    ).fetchall()
    actions = conn.execute(
        """
        SELECT action_id, result_id, action_type, target_url, external_id,
               audit_status, publish_status, created_at
        FROM github_actions
        WHERE task_id = ?
        ORDER BY created_at, action_id
        """,
        (task_id,),
    ).fetchall()
    return {
        "attempt_ids": [row[0] for row in attempts],
        "attempts": [dict(row) for row in attempts],
        "result_ids": [row[0] for row in results],
        "results": [dict(row) for row in results],
        "action_ids": [row[0] for row in actions],
        "actions": [dict(row) for row in actions],
    }


def _episode_task_relations(conn, episode):
    rows = conn.execute(
        """
        SELECT task_id, task_kind, lifecycle, metadata_json
        FROM tasks
        WHERE workstream_id = ?
        ORDER BY created_at, task_id
        """,
        (episode["workstream_id"],),
    ).fetchall()
    task_ids = []
    task_rows = []
    for row in rows:
        metadata = _json_object(row[3])
        if (
            row[0] == episode["last_task_id"]
            or metadata.get("remediation_episode_id") == episode["episode_id"]
        ):
            task_ids.append(row[0])
            task_rows.append(
                {
                    "task_id": row[0],
                    "task_kind": row[1],
                    "lifecycle": row[2],
                }
            )
    attempt_ids = []
    result_ids = []
    action_ids = []
    for task_id in task_ids:
        relations = _task_relations(conn, task_id)
        attempt_ids.extend(relations["attempt_ids"])
        result_ids.extend(relations["result_ids"])
        action_ids.extend(relations["action_ids"])
    return {
        "task_ids": task_ids,
        "tasks": task_rows,
        "attempt_ids": attempt_ids,
        "result_ids": result_ids,
        "action_ids": action_ids,
    }


def load_subject_snapshot(db_path, subject_type, subject_id):
    """Read authoritative Robert state for one workflow subject.

    The operation intentionally opens its own read-only connection and never
    mutates or commits domain state. Callers that are already in a transaction
    should use the execution-state helpers below instead.
    """

    _validate_subject_type(subject_type)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if subject_type == "task":
            task = _load_task(conn, subject_id)
            workstream = _row(
                conn,
                "SELECT * FROM workstreams WHERE workstream_id = ?",
                (task["workstream_id"],),
            )
            binding = _safe_binding(
                execution_store.get_binding(conn, "task", subject_id)
            )
            metadata = _json_object(task["metadata_json"])
            episode_id = metadata.get("remediation_episode_id")
            episode = _load_episode(conn, episode_id) if episode_id else None
            snapshot = {
                "subject_type": "task",
                "subject_id": subject_id,
                "task": _safe_row(task),
                "episode": _safe_row(episode),
                "binding": binding,
                "episode_binding": (
                    _safe_binding(
                        execution_store.get_binding(
                            conn, "remediation_episode", episode_id
                        )
                    )
                    if episode_id
                    else None
                ),
                "workstream": _safe_row(workstream),
                "lifecycle": {
                    "workstream_id": task["workstream_id"],
                    "active_task_id": workstream["active_task_id"]
                    if workstream
                    else None,
                    "episode_id": episode_id,
                    **_task_relations(conn, subject_id),
                },
            }
            return snapshot

        episode = _load_episode(conn, subject_id)
        binding = _safe_binding(
            execution_store.get_binding(conn, "remediation_episode", subject_id)
        )
        relations = _episode_task_relations(conn, episode)
        workstream = _row(
            conn,
            "SELECT * FROM workstreams WHERE workstream_id = ?",
            (episode["workstream_id"],),
        )
        return {
            "subject_type": "remediation_episode",
            "subject_id": subject_id,
            "task": None,
            "episode": _safe_row(episode),
            "binding": binding,
            "workstream": _safe_row(workstream),
            "lifecycle": {
                "workstream_id": episode["workstream_id"],
                "active_task_id": workstream["active_task_id"] if workstream else None,
                "last_task_id": episode["last_task_id"],
                **relations,
            },
        }


def _existing_active_binding(conn, subject_type, subject_id):
    binding = execution_store.get_binding(conn, subject_type, subject_id)
    if not binding:
        return None
    if binding["binding_status"] == "active":
        return binding
    return False


def _claim(conn, subject_type, subject_id, workflow_kind, eligible, now):
    existing = _existing_active_binding(conn, subject_type, subject_id)
    if existing is not None:
        return existing if existing is not False else None
    if not eligible:
        return None
    try:
        return execution_store.ensure_subject(
            conn,
            subject_type,
            subject_id,
            workflow_kind,
            now,
        )
    except sqlite3.IntegrityError:
        # A concurrent claim wins; never reset existing execution control.
        existing = execution_store.get_binding(conn, subject_type, subject_id)
        if existing and existing["binding_status"] == "active":
            return existing
        return None


def claim_subject(conn, subject_type, subject_id, *, now):
    """Claim an eligible subject without reopening stopped execution.

    The caller owns the transaction. An active control may still be returned
    for a terminal domain row while crash recovery finishes; an unbound terminal
    row cannot be newly claimed.
    """

    _validate_subject_type(subject_type)
    if subject_type == "task":
        task = _load_task(conn, subject_id)
        task_kind = task["task_kind"] if isinstance(task, sqlite3.Row) else task[2]
        lifecycle = task["lifecycle"] if isinstance(task, sqlite3.Row) else task[3]
        if task_kind != "human_request":
            raise ValueError("workflow task subject is not an eligible human task")
        return _claim(
            conn,
            "task",
            subject_id,
            "human_request",
            lifecycle not in _TERMINAL_TASK_LIFECYCLES,
            now,
        )

    episode = _load_episode(conn, subject_id)
    episode_status = (
        episode["status"] if isinstance(episode, sqlite3.Row) else episode[7]
    )
    return _claim(
        conn,
        "remediation_episode",
        subject_id,
        "pr_remediation",
        episode_status not in _TERMINAL_EPISODE_STATUSES,
        now,
    )


def _attempt_dispatch_identity(conn, attempt_id):
    row = conn.execute(
        "SELECT task_id, status, metadata_json FROM attempts WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    if row is None:
        return None
    task_id, status, metadata_json = row
    metadata = _json_object(metadata_json)
    dispatch = metadata.get("dispatch")
    if not isinstance(dispatch, dict):
        dispatch = {}
    return {
        "task_id": task_id,
        "status": status,
        "metadata_json": metadata_json,
        "dispatch": dispatch,
    }


def inspect_worker_identity(db_path, attempt_id):
    """Inspect only the recorded dispatch identity for one Attempt."""

    with closing(sqlite3.connect(db_path)) as conn:
        attempt = _attempt_dispatch_identity(conn, attempt_id)
    if not attempt:
        return "unknown"
    return worker_identity.inspect_recorded_identity(attempt["dispatch"])


def terminate_worker(db_path, attempt_id):
    """Terminate and durably record one recorded Worker's outcome."""

    now = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        attempt = _attempt_dispatch_identity(conn, attempt_id)
        if not attempt:
            return "attention"
        try:
            result = worker_identity.terminate_recorded_identity(attempt["dispatch"])
        except Exception as exc:  # pragma: no cover - defensive seam boundary
            result = {
                "status": "attention",
                "reason": "termination_failed",
                "error_type": type(exc).__name__,
            }
        if not isinstance(result, dict):
            result = {"status": "attention", "reason": "invalid_result"}
        result = _safe_metadata(result)
        status = result.get("status", "attention")
        if status not in {"terminated", "already_dead", "attention"}:
            status = "attention"
            result = {**result, "status": status}

        metadata = _json_object(attempt.get("metadata_json"))
        metadata["termination"] = result
        conn.execute(
            "UPDATE attempts SET metadata_json = ? WHERE attempt_id = ?",
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True), attempt_id),
        )

        if status == "attention":
            task_row = conn.execute(
                "SELECT task_kind, metadata_json FROM tasks WHERE task_id = ?",
                (attempt["task_id"],),
            ).fetchone()
            subject_candidates = []
            if task_row:
                task_metadata = _json_object(task_row[1])
                episode_id = task_metadata.get("remediation_episode_id")
                if episode_id:
                    subject_candidates.append(("remediation_episode", episode_id))
            subject_candidates.append(("task", attempt["task_id"]))
            for subject_type, subject_id in subject_candidates:
                binding = execution_store.get_binding(conn, subject_type, subject_id)
                if binding and binding["binding_status"] == "active":
                    execution_store.mark_binding_attention(
                        conn, subject_type, subject_id, now
                    )
                    break

        notification_metadata = {
            "attempt_id": attempt_id,
            "task_id": attempt["task_id"],
            "termination": result,
            "status": status,
        }
        conn.execute(
            """
            INSERT INTO notifications(
              notification_id, task_id, notification_type, channel,
              status, created_at, metadata_json
            )
            VALUES (?, ?, 'worker_termination', 'local', 'recorded', ?, ?)
            ON CONFLICT(notification_id) DO UPDATE SET
              status = excluded.status,
              created_at = excluded.created_at,
              metadata_json = excluded.metadata_json
            """,
            (
                f"notification-worker-termination-{attempt_id}",
                attempt["task_id"],
                now,
                json.dumps(notification_metadata, ensure_ascii=False, sort_keys=True),
            ),
        )
        return status


def _recovery_identity_status(evidence):
    if not isinstance(evidence, dict):
        return None
    for key in ("identity_status", "worker_identity_status", "process_status"):
        value = evidence.get(key)
        if isinstance(value, dict):
            value = value.get("status")
        if value:
            if value in {"not_found", "dead"}:
                return "dead"
            if value in {"alive", "alive_same"}:
                return "alive_same"
            if value in {"unknown", "attention"}:
                return "unknown"
    identity = evidence.get("identity")
    if isinstance(identity, dict):
        value = identity.get("status")
        if value in {"not_found", "dead"}:
            return "dead"
        if value in {"alive", "alive_same"}:
            return "alive_same"
        if value in {"unknown", "attention"}:
            return "unknown"
    return None


def _mark_recovery_attention(conn, binding, now):
    if not binding:
        return
    if binding["binding_status"] in {"active", "attention"}:
        execution_store.mark_binding_attention(
            conn,
            binding["subject_type"],
            binding["subject_id"],
            now,
        )


def _attempt_matches_subject(conn, subject_type, subject_id, attempt_id, task_id):
    if subject_type == "task":
        return task_id == subject_id
    if subject_type != "remediation_episode":
        return False
    row = conn.execute(
        "SELECT metadata_json, workstream_id FROM tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    episode = conn.execute(
        "SELECT workstream_id, last_task_id FROM pr_remediation_episodes WHERE episode_id = ?",
        (subject_id,),
    ).fetchone()
    if not row or not episode:
        return False
    metadata = _json_object(row[0])
    if metadata.get("remediation_episode_id") == subject_id:
        return True
    return row[1] == episode[0] and episode[1] == task_id


def classify_recovery(conn, subject_type, subject_id, attempt_id, evidence, *, now):
    """Classify one dead Worker recovery under the binding's one-shot quota.

    The caller owns the transaction.  No Attempt is created here; the caller
    must create it only after receiving ``recover_once``.
    """

    try:
        _validate_subject_type(subject_type)
    except ValueError:
        return "attention"
    binding = execution_store.get_binding(conn, subject_type, subject_id)
    attempt = _attempt_dispatch_identity(conn, attempt_id)
    identity_status = _recovery_identity_status(evidence)
    logs = evidence.get("logs", {}) if isinstance(evidence, dict) else {}
    terminal_error = retry_policy.worker_error(logs)
    category = retry_policy.error_class(terminal_error)
    if category not in {"transient_failure", "rate_limited"}:
        _mark_recovery_attention(conn, binding, now)
        if binding:
            conn.execute(
                "UPDATE execution_subjects SET metadata_json=? WHERE subject_type=? AND subject_id=?",
                (
                    json.dumps(
                        {
                            **_json_object(binding.get("metadata_json")),
                            "reason_code": category,
                            "required_action": "Repair the reported cause, then retry the task.",
                        }
                    ),
                    subject_type,
                    subject_id,
                ),
            )
        evidence["reason_code"] = category
        return "attention"
    valid_progress = (
        isinstance(evidence, dict)
        and evidence.get("status") == "recoverable_progress"
        and evidence.get("task_id")
        and evidence.get("previous_attempt_id") == attempt_id
        and isinstance(evidence.get("progress_signals"), list)
        and bool(evidence.get("progress_signals"))
    )
    # A structured terminal transport interruption may be retried before any
    # work was saved. It consumes the same one-shot recovery budget.
    valid_progress = valid_progress or bool(
        isinstance(evidence, dict)
        and evidence.get("status") == "transport_interrupted"
        and evidence.get("transport_failure")
        and evidence.get("task_id")
        and evidence.get("previous_attempt_id") == attempt_id
    )
    consistent_attempt = (
        bool(
            attempt
            and attempt["status"] in {"running", "stale", "failed"}
            and _attempt_matches_subject(
                conn,
                subject_type,
                subject_id,
                attempt_id,
                attempt["task_id"],
            )
            and evidence.get("task_id") == attempt["task_id"]
        )
        if isinstance(evidence, dict)
        else False
    )
    owner_ok = bool(binding and binding["binding_status"] == "active")
    if not owner_ok or not consistent_attempt:
        _mark_recovery_attention(conn, binding, now)
        return "attention"
    if identity_status == "alive_same":
        return "continue"
    if identity_status != "dead" or not valid_progress:
        _mark_recovery_attention(conn, binding, now)
        return "attention"
    updated = conn.execute(
        """
        UPDATE execution_subjects
        SET recovery_attempts = recovery_attempts + 1,
            updated_at = ?
        WHERE subject_type = ? AND subject_id = ?
          AND binding_status = 'active'
          AND recovery_attempts = 0
        """,
        (now, subject_type, subject_id),
    )
    if updated.rowcount != 1:
        _mark_recovery_attention(conn, binding, now)
        return "attention"
    return "recover_once"


def publication_subject_for_action(conn, task_id, action_scope):
    """Resolve the workflow subject for a publication without guessing.

    Remediation actions carry their Episode identity in the accepted audit
    scope.  All other actions belong to their ordinary human-request Task.
    The helper intentionally returns a missing binding as ``None``; legacy
    actions remain protected by their intent without fabricating ownership.
    """

    scope = action_scope if isinstance(action_scope, dict) else {}
    task_kind = scope.get("task_kind")
    episode_id = scope.get("episode_id")
    if task_kind in {"ci_remediation", "merge_conflict_remediation"} and episode_id:
        subject_type, subject_id = "remediation_episode", str(episode_id)
    else:
        subject_type, subject_id = "task", str(task_id or "")
    if not subject_id:
        return None, None, None
    binding = execution_store.get_binding(conn, subject_type, subject_id)
    return subject_type, subject_id, binding


def mark_publication_attention(conn, task_id, action_scope, reason_code, *, now):
    """Mark an active owning binding Attention and return its subject.

    This operation is caller-transactional and never reopens terminal or
    already-attention bindings.  A missing binding is a valid legacy state.
    """

    subject_type, subject_id, binding = publication_subject_for_action(
        conn, task_id, action_scope
    )
    if binding and binding.get("binding_status") == "active":
        execution_store.mark_binding_attention(conn, subject_type, subject_id, now)
    return subject_type, subject_id, binding


def publication_scope_is_current(conn, task_id, action_scope):
    """Return whether an accepted publication scope may safely retry absence."""

    subject_type, subject_id, binding = publication_subject_for_action(
        conn, task_id, action_scope
    )
    if binding and binding.get("binding_status") != "active":
        return False
    # Legacy rows may predate workflow bindings.  Their durable publication
    # intent still protects the action; without a binding there is no owner to
    # reopen or transfer, so the authoritative action/task row is sufficient
    # for an absence retry.
    if binding is None:
        return True
    if subject_type == "remediation_episode":
        row = conn.execute(
            "SELECT status FROM pr_remediation_episodes WHERE episode_id = ?",
            (subject_id,),
        ).fetchone()
        return bool(row and row[0] not in _TERMINAL_EPISODE_STATUSES)
    row = conn.execute(
        "SELECT lifecycle FROM tasks WHERE task_id = ?", (subject_id,)
    ).fetchone()
    return bool(row and row[0] not in _TERMINAL_TASK_LIFECYCLES)


def bound_attempt_for_subject(conn, subject_type, subject_id):
    """Return the one mutable Attempt attached to an exact workflow subject."""

    if subject_type == "task":
        row = conn.execute(
            """
            SELECT attempt_id
            FROM attempts
            WHERE task_id = ?
              AND status IN ('prepared', 'running', 'stale')
            ORDER BY attempt_no DESC, attempt_id DESC
            LIMIT 1
            """,
            (subject_id,),
        ).fetchone()
        return row[0] if row else None
    if subject_type != "remediation_episode":
        raise ValueError("unknown workflow subject type")
    row = conn.execute(
        "SELECT last_task_id FROM pr_remediation_episodes WHERE episode_id = ?",
        (subject_id,),
    ).fetchone()
    if not row or not row[0]:
        return None
    task_id = row[0]
    attempt = conn.execute(
        """
        SELECT attempt_id
        FROM attempts
        WHERE task_id = ?
          AND status IN ('prepared', 'running', 'stale')
        ORDER BY attempt_no DESC, attempt_id DESC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    return attempt[0] if attempt else None


def apply_classified_fact(conn, subject_type, subject_id, fact, *, now):
    """Apply a classified fact and its dedupe receipt in the caller transaction.

    Bounded evidence is persisted with the execution state. Historical fact
    replay returns its receipt without replacing newer evidence.
    """

    from robert_agent import workflow_events

    binding = execution_store.get_binding(conn, subject_type, subject_id)
    if not binding:
        raise ValueError("workflow subject is not bound")
    classification, evidence = workflow_events.classify_and_normalize(fact)
    if evidence["subject_type"] != subject_type or evidence["subject_id"] != subject_id:
        raise ValueError("workflow fact subject does not match its binding")
    metadata = _json_object(binding.get("metadata_json"))
    latest = metadata.get("latest_fact")
    if isinstance(latest, dict) and latest.get("fact_id"):
        conn.execute(
            "INSERT OR IGNORE INTO execution_fact_receipts VALUES (?,?,?,?)",
            (
                subject_type,
                subject_id,
                latest["fact_id"],
                json.dumps(latest, sort_keys=True),
            ),
        )
    recorded = conn.execute(
        "SELECT evidence_json FROM execution_fact_receipts WHERE subject_type=? AND subject_id=? AND fact_id=?",
        (subject_type, subject_id, evidence["fact_id"]),
    ).fetchone()
    if recorded:
        if recorded[0] is not None and json.loads(recorded[0]) != evidence:
            raise ValueError("workflow fact id conflicts with recorded evidence")
        return {
            "classification": classification,
            "fact_id": evidence["fact_id"],
            "evidence": latest if isinstance(latest, dict) else evidence,
            "duplicate": True,
            "binding_status": binding["binding_status"],
            "signal_id": None,
            "attempt_id": bound_attempt_for_subject(conn, subject_type, subject_id),
        }
    if isinstance(latest, dict) and latest.get("fact_id") == evidence["fact_id"]:
        if latest != evidence:
            raise ValueError("workflow fact id conflicts with recorded evidence")
        return {
            "classification": classification,
            "fact_id": evidence["fact_id"],
            "evidence": evidence,
            "duplicate": True,
            "binding_status": binding["binding_status"],
            "signal_id": None,
            "attempt_id": bound_attempt_for_subject(conn, subject_type, subject_id),
        }
    if binding["binding_status"] != "active":
        raise ValueError("workflow subject binding is not active")

    conn.execute(
        "INSERT INTO execution_fact_receipts VALUES (?,?,?,?)",
        (
            subject_type,
            subject_id,
            evidence["fact_id"],
            json.dumps(evidence, sort_keys=True),
        ),
    )
    metadata["latest_fact"] = evidence
    conn.execute(
        "UPDATE execution_subjects SET metadata_json = ?, updated_at = ? WHERE subject_type = ? AND subject_id = ?",
        (
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            now,
            subject_type,
            subject_id,
        ),
    )

    signal_id = None

    if classification == workflow_events.TERMINAL:
        execution_store.close_binding(conn, subject_type, subject_id, now)
    elif classification == workflow_events.INVALIDATING:
        execution_store.mark_binding_attention(conn, subject_type, subject_id, now)

    return {
        "classification": classification,
        "fact_id": evidence["fact_id"],
        "evidence": evidence,
        "duplicate": False,
        "binding_status": (
            "terminal"
            if classification == workflow_events.TERMINAL
            else "attention"
            if classification == workflow_events.INVALIDATING
            else "active"
        ),
        "signal_id": signal_id,
        "attempt_id": bound_attempt_for_subject(conn, subject_type, subject_id),
    }
