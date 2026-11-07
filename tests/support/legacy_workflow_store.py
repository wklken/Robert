"""Historical DBOS-format fixture helpers; not part of Robert runtime."""

import hashlib
import json


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


def bind_subject(
    conn,
    subject_type,
    subject_id,
    workflow_kind,
    owner_engine,
    now,
    *,
    definition_version="v1",
):
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
    workflow_id, shadow_workflow_id = _workflow_ids(
        subject_type, subject_id, owner_engine, definition_version
    )
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


def enqueue_signal(
    conn, workflow_id, topic, dedupe_key, payload, now, not_before_at=None
):
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
        raise ValueError(
            "workflow signal dedupe key conflicts with a different payload"
        )
    signal_id = signal_id_for(workflow_id, topic, dedupe_key)
    conn.execute(
        """
        INSERT INTO workflow_outbox(
          signal_id, workflow_id, topic, dedupe_key, payload_json,
          not_before_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal_id,
            workflow_id,
            topic,
            dedupe_key,
            payload_json,
            not_before_at or now,
            now,
        ),
    )
    return _row(
        conn.execute("SELECT * FROM workflow_outbox WHERE signal_id = ?", (signal_id,))
    )
