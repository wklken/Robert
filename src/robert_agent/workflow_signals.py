"""Transactional Robert-to-DBOS subject signal delivery."""

from contextlib import closing
import json
import sqlite3

from robert_agent import workflow_store


DBOS_TOPIC = "subject-events"
_MESSAGE_KEYS = {"signal_id", "topic", "subject_type", "subject_id", "fact_id"}


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _required_id(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"workflow signal {name} must be a non-empty string")
    return value


def record_subject_signal(conn, binding, topic, dedupe_key, payload, now):
    """Insert one ID-only subject signal using the caller's transaction."""

    if not isinstance(binding, dict):
        raise ValueError("workflow binding must be a mapping")
    if binding.get("owner_engine") != "dbos":
        raise ValueError("workflow signal requires a DBOS-owned binding")
    if binding.get("binding_status", "active") != "active":
        raise ValueError("workflow signal requires an active binding")
    workflow_id = _required_id(binding.get("workflow_id"), "workflow_id")
    subject_type = _required_id(binding.get("subject_type"), "subject_type")
    subject_id = _required_id(binding.get("subject_id"), "subject_id")
    topic = _required_id(topic, "topic")
    dedupe_key = _required_id(dedupe_key, "dedupe_key")
    if not isinstance(payload, dict) or set(payload) != {"fact_id"}:
        raise ValueError("workflow signal payload must contain only fact_id")
    fact_id = _required_id(payload.get("fact_id"), "fact_id")
    signal_id = workflow_store.signal_id_for(workflow_id, topic, dedupe_key)
    message = {
        "signal_id": signal_id,
        "topic": topic,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "fact_id": fact_id,
    }
    return workflow_store.enqueue_signal(
        conn,
        workflow_id,
        topic,
        dedupe_key,
        message,
        now,
    )


def _message_for_signal(conn, signal):
    try:
        message = json.loads(signal["payload_json"])
    except (TypeError, ValueError) as exc:
        raise ValueError("workflow signal payload is invalid JSON") from exc
    if not isinstance(message, dict) or set(message) != _MESSAGE_KEYS:
        raise ValueError("workflow signal payload has an invalid shape")
    for key in _MESSAGE_KEYS:
        _required_id(message.get(key), key)
    if message["signal_id"] != signal["signal_id"]:
        raise ValueError("workflow signal payload ID conflicts with its row")
    if message["topic"] != signal["topic"]:
        raise ValueError("workflow signal payload topic conflicts with its row")
    binding = conn.execute(
        """
        SELECT subject_type, subject_id, workflow_id, owner_engine, binding_status,
               metadata_json
        FROM workflow_bindings
        WHERE workflow_id = ?
        """,
        (signal["workflow_id"],),
    ).fetchone()
    if not binding:
        raise ValueError("workflow signal target binding does not exist")
    if binding[2] != signal["workflow_id"] or binding[3] != "dbos":
        raise ValueError("workflow signal target binding conflicts with its row")
    if binding[0] != message["subject_type"] or binding[1] != message["subject_id"]:
        raise ValueError("workflow signal subject conflicts with its binding")
    if binding[4] == "attention":
        try:
            metadata = json.loads(binding[5] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        latest = metadata.get("latest_fact") if isinstance(metadata, dict) else None
        if not (
            isinstance(latest, dict)
            and latest.get("fact_id") == message["fact_id"]
            and latest.get("classification") == "invalidating"
        ):
            raise ValueError("workflow signal target binding requires attention")
    return message


def _empty_result():
    return {
        "ok": True,
        "processed": 0,
        "sent": 0,
        "delivered": 0,
        "failed": 0,
        "retried": 0,
        "attention": 0,
    }


def _mark_validation_attention(db_path, signal, now):
    with closing(_connect(db_path)) as conn, conn:
        workflow_store.mark_signal_attention(
            conn,
            signal["signal_id"],
            now,
            exception_class="ValueError",
        )
        binding = conn.execute(
            """
            SELECT subject_type, subject_id, owner_engine, binding_status
            FROM workflow_bindings
            WHERE workflow_id = ?
            """,
            (signal["workflow_id"],),
        ).fetchone()
        if binding and binding[2] == "dbos" and binding[3] != "terminal":
            workflow_store.mark_binding_attention(
                conn,
                binding[0],
                binding[1],
                now,
            )


def relay_due_signals(db_path, dbos_sender, *, limit, now, repo_ids=None):
    """Relay due outbox rows from the daemon parent process.

    Sending occurs outside SQLite transactions.  A successful send is marked
    delivered in a new transaction, so a crash between those operations can
    result in a harmless duplicate DBOS message.
    """

    result = _empty_result()
    if dbos_sender is None or limit <= 0:
        return result
    with closing(_connect(db_path)) as conn:
        signals = workflow_store.list_due_signals(
            conn,
            now,
            limit=limit,
            repo_ids=repo_ids,
        )
    for signal in signals:
        result["processed"] += 1
        try:
            with closing(_connect(db_path)) as conn:
                message = _message_for_signal(conn, signal)
        except ValueError:
            _mark_validation_attention(db_path, signal, now)
            result["attention"] += 1
            result["ok"] = False
            continue
        try:
            dbos_sender(
                signal["workflow_id"],
                message,
                topic=DBOS_TOPIC,
                idempotency_key=signal["signal_id"],
            )
        except Exception as exc:
            with closing(_connect(db_path)) as conn, conn:
                workflow_store.mark_signal_retry(
                    conn,
                    signal["signal_id"],
                    now,
                    type(exc).__name__,
                )
            result["failed"] += 1
            result["retried"] += 1
            continue
        try:
            with closing(_connect(db_path)) as conn, conn:
                workflow_store.mark_signal_delivered(conn, signal["signal_id"], now)
        except ValueError:
            result["ok"] = False
            result["attention"] += 1
            continue
        result["sent"] += 1
        result["delivered"] += 1
    return result
