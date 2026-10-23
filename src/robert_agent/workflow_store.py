"""Caller-transaction workflow ownership and durable signal storage helpers."""

import hashlib
import json
from datetime import datetime, timedelta, timezone


_TERMINAL_TASK_LIFECYCLES = {"completed", "failed", "canceled", "ignored"}
_TERMINAL_EPISODE_STATUSES = {"resolved", "exhausted", "superseded", "canceled"}
_PUBLICATION_TRANSITIONS = {
    "prepared": {"executing"},
    "executing": {"published", "confirmed_absent", "failed", "attention"},
    "confirmed_absent": {"executing"},
    "failed": {"executing"},
    "published": set(),
    "attention": set(),
}


def _row(cursor):
    value = cursor.fetchone()
    if value is None:
        return None
    return dict(zip((column[0] for column in cursor.description), value))


def _json(value):
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


def _id(prefix, *values):
    digest = hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def signal_id_for(workflow_id, topic, dedupe_key):
    return _id("signal", workflow_id, topic, dedupe_key)


def _workflow_ids(subject_type, subject_id, owner_engine, version="v1"):
    if subject_type == "task":
        active = f"robert:{version}:task:{subject_id}"
    else:
        active = f"robert:{version}:remediation:{subject_id}"
    shadow = f"robert:shadow:{version}:{subject_type}:{subject_id}"
    return (active if owner_engine == "dbos" else None), shadow


def _validate_subject(conn, subject_type, subject_id, workflow_kind):
    legal_pairs = {
        ("task", "human_request"),
        ("remediation_episode", "pr_remediation"),
    }
    if (subject_type, workflow_kind) not in legal_pairs:
        raise ValueError("illegal workflow subject and kind pairing")
    if subject_type == "task":
        row = conn.execute(
            "SELECT task_kind, lifecycle FROM tasks WHERE task_id = ?", (subject_id,)
        ).fetchone()
        if not row:
            raise ValueError("workflow task subject does not exist")
        if row[0] != "human_request" or row[1] in _TERMINAL_TASK_LIFECYCLES:
            raise ValueError("workflow task subject is not eligible")
        return
    row = conn.execute(
        "SELECT status FROM pr_remediation_episodes WHERE episode_id = ?", (subject_id,)
    ).fetchone()
    if not row:
        raise ValueError("workflow remediation episode subject does not exist")
    if row[0] in _TERMINAL_EPISODE_STATUSES:
        raise ValueError("workflow remediation episode subject is not eligible")


def get_binding(conn, subject_type, subject_id):
    return _row(
        conn.execute(
            "SELECT * FROM workflow_bindings WHERE subject_type = ? AND subject_id = ?",
            (subject_type, subject_id),
        )
    )


def bind_subject(conn, subject_type, subject_id, workflow_kind, owner_engine, now, *, definition_version="v1"):
    if definition_version not in {"v1", "v2"}:
        raise ValueError("unsupported workflow definition version")
    if owner_engine not in {"legacy", "dbos"}:
        raise ValueError("unknown workflow owner engine")
    if (subject_type, workflow_kind) not in {
        ("task", "human_request"),
        ("remediation_episode", "pr_remediation"),
    }:
        raise ValueError("illegal workflow subject and kind pairing")
    existing = get_binding(conn, subject_type, subject_id)
    workflow_id, shadow_workflow_id = _workflow_ids(subject_type, subject_id, owner_engine, definition_version)
    if existing:
        if (
            existing["workflow_kind"] == workflow_kind
            and existing["owner_engine"] == owner_engine
            and existing["workflow_id"] == workflow_id
            and existing["shadow_workflow_id"] == shadow_workflow_id
        ):
            return existing
        raise ValueError("workflow subject is already bound with conflicting ownership")
    _validate_subject(conn, subject_type, subject_id, workflow_kind)
    conn.execute(
        """
        INSERT INTO workflow_bindings(
          subject_type, subject_id, workflow_kind, owner_engine, workflow_id,
          shadow_workflow_id, binding_status, created_at, updated_at, definition_version
        ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
        """,
        (
            subject_type,
            subject_id,
            workflow_kind,
            owner_engine,
            workflow_id,
            shadow_workflow_id,
            now,
            now,
            definition_version,
        ),
    )
    return get_binding(conn, subject_type, subject_id)


def ensure_legacy_binding(conn, subject_type, subject_id, workflow_kind, now):
    return bind_subject(conn, subject_type, subject_id, workflow_kind, "legacy", now)


def assert_owner(conn, subject_type, subject_id, expected_engine):
    binding = get_binding(conn, subject_type, subject_id)
    if (
        not binding
        or binding["owner_engine"] != expected_engine
        or binding["binding_status"] != "active"
    ):
        raise ValueError("workflow subject is not owned by the expected engine")


def close_binding(conn, subject_type, subject_id, now):
    binding = get_binding(conn, subject_type, subject_id)
    if not binding:
        raise ValueError("workflow subject is not bound")
    if binding["binding_status"] == "terminal":
        return binding
    conn.execute(
        """
        UPDATE workflow_bindings
        SET binding_status = 'terminal', terminal_at = ?, updated_at = ?
        WHERE subject_type = ? AND subject_id = ?
        """,
        (now, now, subject_type, subject_id),
    )
    return get_binding(conn, subject_type, subject_id)


def mark_binding_attention(conn, subject_type, subject_id, now):
    binding = get_binding(conn, subject_type, subject_id)
    if not binding:
        raise ValueError("workflow subject is not bound")
    if binding["binding_status"] == "attention":
        return binding
    if binding["binding_status"] == "terminal":
        raise ValueError("terminal workflow binding cannot require attention")
    conn.execute(
        """
        UPDATE workflow_bindings
        SET binding_status = 'attention', updated_at = ?
        WHERE subject_type = ? AND subject_id = ?
        """,
        (now, subject_type, subject_id),
    )
    return get_binding(conn, subject_type, subject_id)


def enqueue_signal(conn, workflow_id, topic, dedupe_key, payload, now, not_before_at=None):
    payload_json = _json(payload)
    existing = _row(
        conn.execute(
            """
            SELECT * FROM workflow_outbox
            WHERE workflow_id = ? AND topic = ? AND dedupe_key = ?
            """,
            (workflow_id, topic, dedupe_key),
        )
    )
    if existing:
        if existing["payload_json"] == payload_json:
            return existing
        raise ValueError("workflow signal dedupe key conflicts with a different payload")
    signal_id = signal_id_for(workflow_id, topic, dedupe_key)
    conn.execute(
        """
        INSERT INTO workflow_outbox(
          signal_id, workflow_id, topic, dedupe_key, payload_json,
          not_before_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (signal_id, workflow_id, topic, dedupe_key, payload_json, not_before_at or now, now),
    )
    return _row(conn.execute("SELECT * FROM workflow_outbox WHERE signal_id = ?", (signal_id,)))


def list_due_signals(conn, now, limit=100, repo_ids=None):
    repo_ids = list(repo_ids or [])
    if repo_ids:
        placeholders = ",".join("?" for _repo_id in repo_ids)
        cursor = conn.execute(
            f"""
            SELECT o.*
            FROM workflow_outbox o
            JOIN workflow_bindings b ON b.workflow_id = o.workflow_id
            LEFT JOIN tasks t
              ON b.subject_type = 'task' AND t.task_id = b.subject_id
            LEFT JOIN pr_remediation_episodes e
              ON b.subject_type = 'remediation_episode' AND e.episode_id = b.subject_id
            JOIN workstreams w
              ON w.workstream_id = COALESCE(t.workstream_id, e.workstream_id)
            WHERE o.status = 'pending'
              AND o.not_before_at <= ?
              AND w.repo_id IN ({placeholders})
            ORDER BY o.not_before_at, o.created_at, o.signal_id
            LIMIT ?
            """,
            [now, *repo_ids, limit],
        )
    else:
        cursor = conn.execute(
            """
            SELECT * FROM workflow_outbox
            WHERE status = 'pending' AND not_before_at <= ?
            ORDER BY not_before_at, created_at, signal_id
            LIMIT ?
            """,
            (now, limit),
        )
    return [
        dict(zip((column[0] for column in cursor.description), value))
        for value in cursor.fetchall()
    ]


def mark_signal_delivered(conn, signal_id, now):
    signal = _row(conn.execute("SELECT * FROM workflow_outbox WHERE signal_id = ?", (signal_id,)))
    if not signal:
        raise ValueError("workflow signal does not exist")
    if signal["status"] == "delivered":
        return signal
    if signal["status"] != "pending":
        raise ValueError("workflow signal is not pending")
    conn.execute(
        """
        UPDATE workflow_outbox
        SET status = 'delivered', delivered_at = ?, attempt_count = attempt_count + 1
        WHERE signal_id = ?
        """,
        (now, signal_id),
    )
    return _row(conn.execute("SELECT * FROM workflow_outbox WHERE signal_id = ?", (signal_id,)))


def mark_signal_retry(conn, signal_id, now, exception_class):
    """Record a transient delivery failure in the caller's transaction."""

    signal = _row(conn.execute("SELECT * FROM workflow_outbox WHERE signal_id = ?", (signal_id,)))
    if not signal:
        raise ValueError("workflow signal does not exist")
    if signal["status"] != "pending":
        raise ValueError("workflow signal is not pending")
    attempt_count = int(signal["attempt_count"]) + 1
    delay_seconds = min(60, 2 ** min(attempt_count, 6))
    try:
        parsed_now = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("workflow signal retry timestamp is invalid") from exc
    if parsed_now.tzinfo is None:
        parsed_now = parsed_now.replace(tzinfo=timezone.utc)
    not_before_at = (parsed_now + timedelta(seconds=delay_seconds)).isoformat()
    error_json = _json({"exception_class": str(exception_class)})
    conn.execute(
        """
        UPDATE workflow_outbox
        SET attempt_count = ?, not_before_at = ?, last_error_json = ?
        WHERE signal_id = ?
        """,
        (attempt_count, not_before_at, error_json, signal_id),
    )
    if attempt_count >= 3 or (parsed_now - datetime.fromisoformat(signal["created_at"].replace('Z', '+00:00'))).total_seconds() >= 600:
        signal = mark_signal_attention(conn, signal_id, now, exception_class)
        binding = _row(conn.execute("SELECT * FROM workflow_bindings WHERE workflow_id=?", (signal["workflow_id"],)))
        if binding and binding['binding_status'] != 'terminal':
            mark_binding_attention(conn, binding['subject_type'], binding['subject_id'], now)
            task_id = binding['subject_id'] if binding['subject_type'] == 'task' else None
            conn.execute("INSERT OR IGNORE INTO notifications(notification_id, task_id, notification_type, channel, status, created_at, metadata_json) VALUES (?,?,'workflow_delivery_exhausted','local','recorded',?,?)",
                ('notification-' + signal_id, task_id, now, _json({'signal_id': signal_id, 'reason_code':'retry_budget_exhausted', 'attempt_count':attempt_count, 'required_action':'Restore workflow connectivity and explicitly recover this task.'})))
        return signal
    return _row(conn.execute("SELECT * FROM workflow_outbox WHERE signal_id = ?", (signal_id,)))


def mark_signal_attention(conn, signal_id, now, exception_class="ValueError"):
    """Stop delivery after a local validation conflict."""

    signal = _row(conn.execute("SELECT * FROM workflow_outbox WHERE signal_id = ?", (signal_id,)))
    if not signal:
        raise ValueError("workflow signal does not exist")
    if signal["status"] == "attention":
        return signal
    if signal["status"] == "delivered":
        raise ValueError("delivered workflow signal cannot require attention")
    conn.execute(
        """
        UPDATE workflow_outbox
        SET status = 'attention', last_error_json = ?
        WHERE signal_id = ?
        """,
        (_json({"exception_class": str(exception_class)}), signal_id),
    )
    return _row(conn.execute("SELECT * FROM workflow_outbox WHERE signal_id = ?", (signal_id,)))


def consume_signal_once(conn, signal_id, workflow_id, topic, now):
    existing = _row(
        conn.execute(
            "SELECT * FROM workflow_signal_receipts WHERE signal_id = ?",
            (signal_id,),
        )
    )
    if existing:
        if existing["workflow_id"] == workflow_id and existing["topic"] == topic:
            return False
        raise ValueError("workflow signal receipt conflicts with its original target")
    cursor = conn.execute(
        """
        INSERT INTO workflow_signal_receipts(signal_id, workflow_id, topic, consumed_at)
        VALUES (?, ?, ?, ?) ON CONFLICT(signal_id) DO NOTHING
        """,
        (signal_id, workflow_id, topic, now),
    )
    return cursor.rowcount == 1


def prepare_publication_intent(conn, action_id, idempotency_key, request_fingerprint, now):
    if not conn.execute("SELECT 1 FROM github_actions WHERE action_id = ?", (action_id,)).fetchone():
        raise ValueError("publication action does not exist")
    existing = _row(
        conn.execute(
            "SELECT * FROM publication_intents WHERE action_id = ?", (action_id,)
        )
    )
    if existing:
        if (
            existing["idempotency_key"] == idempotency_key
            and existing["request_fingerprint"] == request_fingerprint
        ):
            return existing
        raise ValueError("publication action already has a conflicting intent")
    existing_key = _row(
        conn.execute(
            "SELECT * FROM publication_intents WHERE idempotency_key = ?",
            (idempotency_key,),
        )
    )
    if existing_key:
        raise ValueError("publication idempotency key already belongs to another action")
    publication_id = _id("publication", action_id)
    conn.execute(
        """
        INSERT INTO publication_intents(
          publication_id, action_id, idempotency_key, request_fingerprint,
          status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'prepared', ?, ?)
        """,
        (publication_id, action_id, idempotency_key, request_fingerprint, now, now),
    )
    return _row(
        conn.execute("SELECT * FROM publication_intents WHERE publication_id = ?", (publication_id,))
    )


def transition_publication_intent(
    conn, publication_id, status, now, remote_evidence=None, error=None
):
    intent = _row(
        conn.execute("SELECT * FROM publication_intents WHERE publication_id = ?", (publication_id,))
    )
    if not intent:
        raise ValueError("publication intent does not exist")
    if intent["status"] == status:
        return intent
    if status not in _PUBLICATION_TRANSITIONS.get(intent["status"], set()):
        raise ValueError(
            "illegal publication intent transition: "
            f"{intent['status']} -> {status}"
        )
    fields = ["status = ?", "updated_at = ?"]
    values = [status, now]
    if status == "executing":
        fields.append("started_at = COALESCE(started_at, ?)")
        values.append(now)
        fields.append("finished_at = NULL")
        fields.append("error_json = NULL")
    if status in {"published", "confirmed_absent", "failed", "attention"}:
        fields.append("finished_at = ?")
        values.append(now)
    if remote_evidence is not None:
        fields.append("remote_evidence_json = ?")
        values.append(_json(remote_evidence))
    if error is not None:
        fields.append("error_json = ?")
        values.append(_json(error))
    values.append(publication_id)
    conn.execute(
        f"UPDATE publication_intents SET {', '.join(fields)} WHERE publication_id = ?",
        values,
    )
    return _row(
        conn.execute("SELECT * FROM publication_intents WHERE publication_id = ?", (publication_id,))
    )
