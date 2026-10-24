"""Subject snapshots and the caller-owned legacy ownership fence."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Protocol

from robert_agent import worker_identity, retry_policy
from robert_agent import workflow_store


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


class HumanRequestDriver(Protocol):
    """The four operations the deterministic human-request controller needs."""

    def next_transition(self, task_id: str):
        ...

    def apply_transition(self, task_id: str, transition):
        ...

    def consume_signal(self, task_id: str, message):
        ...

    def mark_attention(self, task_id: str, reason_code: str):
        ...


class RemediationDriver(Protocol):
    """The four operations the deterministic remediation controller needs."""

    def next_transition(self, episode_id: str):
        ...

    def apply_transition(self, episode_id: str, transition):
        ...

    def consume_signal(self, episode_id: str, message):
        ...

    def mark_attention(self, episode_id: str, reason_code: str):
        ...


_HUMAN_REQUEST_DRIVER = None
_REMEDIATION_DRIVER = None
_HUMAN_REQUEST_OPERATIONS = {
    "prepare",
    "dispatch",
    "supervise",
    "audit",
    "publish",
    "finalize",
    "create_followup",
    "wait",
    "terminal",
}
_HUMAN_REQUEST_SIGNAL_KEYS = {
    "signal_id",
    "topic",
    "subject_type",
    "subject_id",
    "fact_id",
}
_HUMAN_REQUEST_APPLY_KEYS = {
    "applied_transition_id",
    "status",
    "state",
}
_HUMAN_REQUEST_CONSUME_KEYS = {"signal_id", "status", "state"}
_HUMAN_REQUEST_APPLY_STATUSES = {"applied", "already_applied", "attention"}
_HUMAN_REQUEST_CONSUME_STATUSES = {"consumed", "duplicate", "attention"}
HUMAN_REQUEST_TERMINAL_STATUSES = {
    "completed",
    "rejected",
    "invalidated",
    "canceled",
    "attention",
    "failed",
}
_HUMAN_REQUEST_SENSITIVE_STATE_TOKENS = (
    "prompt",
    "handoff",
    "credential",
    "password",
    "secret",
    "token",
    "authorization",
    "api_key",
    "private_key",
    "database",
)
_HUMAN_REQUEST_SENSITIVE_VALUE_MARKERS = (
    "://",
    "bearer ",
    "basic ",
    "authorization:",
    "password=",
    "password:",
    "-----begin",
    "-----end",
)


def configure_human_request_driver(driver: HumanRequestDriver):
    """Configure the process-local v1 driver exactly once."""

    global _HUMAN_REQUEST_DRIVER
    if driver is None:
        raise ValueError("HumanRequestDriver must not be None")
    missing = [
        name
        for name in (
            "next_transition",
            "apply_transition",
            "consume_signal",
            "mark_attention",
        )
        if not callable(getattr(driver, name, None))
    ]
    if missing:
        raise TypeError(
            "HumanRequestDriver is missing required method(s): " + ", ".join(missing)
        )
    if _HUMAN_REQUEST_DRIVER is None:
        _HUMAN_REQUEST_DRIVER = driver
        return driver
    if _HUMAN_REQUEST_DRIVER is driver:
        return driver
    raise ValueError("HumanRequestDriver is already configured with a conflicting driver")


def reset_human_request_driver():
    """Reset the process-local driver for tests and DBOS process teardown."""

    global _HUMAN_REQUEST_DRIVER
    _HUMAN_REQUEST_DRIVER = None


def current_human_request_driver() -> HumanRequestDriver:
    if _HUMAN_REQUEST_DRIVER is None:
        raise RuntimeError("HumanRequestDriver is not configured")
    return _HUMAN_REQUEST_DRIVER


def configure_remediation_driver(driver: RemediationDriver):
    """Configure the process-local remediation driver exactly once."""

    global _REMEDIATION_DRIVER
    if driver is None:
        raise ValueError("RemediationDriver must not be None")
    missing = [
        name
        for name in (
            "next_transition",
            "apply_transition",
            "consume_signal",
            "mark_attention",
        )
        if not callable(getattr(driver, name, None))
    ]
    if missing:
        raise TypeError(
            "RemediationDriver is missing required method(s): " + ", ".join(missing)
        )
    if _REMEDIATION_DRIVER is None:
        _REMEDIATION_DRIVER = driver
        return driver
    if _REMEDIATION_DRIVER is driver:
        return driver
    raise ValueError("RemediationDriver is already configured with a conflicting driver")


def reset_remediation_driver():
    """Reset the process-local remediation driver for tests and teardown."""

    global _REMEDIATION_DRIVER
    _REMEDIATION_DRIVER = None


def current_remediation_driver() -> RemediationDriver:
    if _REMEDIATION_DRIVER is None:
        raise RuntimeError("RemediationDriver is not configured")
    return _REMEDIATION_DRIVER


def _required_identifier(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"human request {name} must be a non-empty string")
    return value


def _validate_state_value(value, *, depth=0):
    if depth > 3:
        raise ValueError("human request transition state is too deeply nested")
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str):
            if len(value) > 256:
                raise ValueError("human request transition state contains an unbounded value")
            if any(marker in value.lower() for marker in _HUMAN_REQUEST_SENSITIVE_VALUE_MARKERS):
                raise ValueError("human request transition state contains sensitive data")
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("human request transition state contains a non-finite value")
        return value
    if isinstance(value, list):
        if len(value) > 32:
            raise ValueError("human request transition state contains an unbounded list")
        return [_validate_state_value(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > 32:
            raise ValueError("human request transition state contains an unbounded mapping")
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 64:
                raise ValueError("human request transition state keys must be bounded strings")
            normalized = key.lower().replace("-", "_")
            if any(token in normalized for token in _HUMAN_REQUEST_SENSITIVE_STATE_TOKENS):
                raise ValueError("human request transition state contains sensitive data")
            result[key] = _validate_state_value(item, depth=depth + 1)
        return result
    raise ValueError("human request transition state must be serializable")


def validate_human_request_state(state):
    """Return a bounded, redacted, serializable state mapping."""

    if not isinstance(state, dict):
        raise ValueError("human request transition state must be a mapping")
    result = _validate_state_value(state)
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(encoded.encode("utf-8")) > 4096:
        raise ValueError("human request transition state is too large")
    return result


def validate_human_request_transition(transition):
    """Validate and copy the exact v1 transition payload."""

    if not isinstance(transition, dict) or set(transition) != {
        "transition_id",
        "operation",
        "wait_seconds",
        "terminal",
        "state",
    }:
        raise ValueError("human request transition has an invalid shape")
    transition_id = _required_identifier(transition["transition_id"], "transition_id")
    operation = transition["operation"]
    if not isinstance(operation, str) or operation not in _HUMAN_REQUEST_OPERATIONS:
        raise ValueError("human request transition has an invalid operation")
    terminal = transition["terminal"]
    if type(terminal) is not bool:
        raise ValueError("human request transition terminal must be a boolean")
    if operation == "terminal" and not terminal:
        raise ValueError("terminal operation must set terminal=true")
    if operation != "terminal" and terminal:
        raise ValueError("non-terminal operation must set terminal=false")
    wait_seconds = transition["wait_seconds"]
    if operation == "wait":
        if type(wait_seconds) is not int or not 1 <= wait_seconds <= 300:
            raise ValueError("wait transition seconds must be an integer from 1 to 300")
    elif wait_seconds is not None:
        raise ValueError("wait_seconds is only valid for wait transitions")
    state = validate_human_request_state(transition["state"])
    if operation == "audit":
        action_count = state.get("action_count")
        if type(action_count) is not int or action_count < 0:
            raise ValueError("audit transition requires a non-negative action_count")
    if operation == "terminal":
        status = state.get("status")
        if not isinstance(status, str) or status not in HUMAN_REQUEST_TERMINAL_STATUSES:
            raise ValueError("terminal transition requires an allowed status")
    return {
        "transition_id": transition_id,
        "operation": operation,
        "wait_seconds": wait_seconds,
        "terminal": terminal,
        "state": state,
    }


def validate_human_request_signal(task_id, message):
    """Validate the existing WU5 ID-only signal contract for one Task."""

    if not isinstance(message, dict) or set(message) != _HUMAN_REQUEST_SIGNAL_KEYS:
        raise ValueError("human request signal has an invalid shape")
    for key in _HUMAN_REQUEST_SIGNAL_KEYS:
        _required_identifier(message[key], key)
    # All messages use the subject-events transport queue. Worker result facts
    # retain their own payload topic and persisted outbox/deduplication identity.
    if message["topic"] not in {"subject-events", "worker_result"}:
        raise ValueError("human request signal topic is invalid")
    if message["subject_type"] != "task" or message["subject_id"] != task_id:
        raise ValueError("human request signal subject conflicts with its workflow")
    return {key: message[key] for key in _HUMAN_REQUEST_SIGNAL_KEYS}


def validate_human_request_apply_result(transition, result):
    if not isinstance(result, dict) or set(result) != _HUMAN_REQUEST_APPLY_KEYS:
        raise ValueError("human request apply result has an invalid shape")
    applied_id = _required_identifier(result["applied_transition_id"], "applied_transition_id")
    if applied_id != transition["transition_id"]:
        raise ValueError("human request apply result conflicts with its transition")
    if (
        not isinstance(result["status"], str)
        or result["status"] not in _HUMAN_REQUEST_APPLY_STATUSES
    ):
        raise ValueError("human request apply result has an invalid status")
    return {
        "applied_transition_id": applied_id,
        "status": result["status"],
        "state": validate_human_request_state(result["state"]),
    }


def validate_human_request_consume_result(message, result):
    if not isinstance(result, dict) or set(result) != _HUMAN_REQUEST_CONSUME_KEYS:
        raise ValueError("human request consume result has an invalid shape")
    signal_id = _required_identifier(result["signal_id"], "signal_id")
    if signal_id != message["signal_id"]:
        raise ValueError("human request consume result conflicts with its signal")
    if (
        not isinstance(result["status"], str)
        or result["status"] not in _HUMAN_REQUEST_CONSUME_STATUSES
    ):
        raise ValueError("human request consume result has an invalid status")
    return {
        "signal_id": signal_id,
        "status": result["status"],
        "state": validate_human_request_state(result["state"]),
    }


_REMEDIATION_OPERATIONS = {
    "validate_snapshot",
    "enforce_budget",
    "prepare",
    "dispatch",
    "supervise",
    "audit",
    "publish",
    "wait_health",
    "resolve",
    "exhaust",
    "supersede",
    "wait",
    "terminal",
}
_REMEDIATION_SNAPSHOT_STATUSES = {
    "current",
    "superseded",
    "terminal",
    "unknown",
}
REMEDIATION_TERMINAL_STATUSES = {
    "resolved",
    "exhausted",
    "superseded",
    "canceled",
    "attention",
    "failed",
}
_REMEDIATION_SIGNAL_KEYS = {
    "signal_id",
    "topic",
    "subject_type",
    "subject_id",
    "fact_id",
}
_REMEDIATION_APPLY_KEYS = {
    "applied_transition_id",
    "status",
    "state",
}
_REMEDIATION_CONSUME_KEYS = {"signal_id", "status", "state"}
_REMEDIATION_APPLY_STATUSES = {"applied", "already_applied", "attention"}
_REMEDIATION_CONSUME_STATUSES = {"consumed", "duplicate", "attention"}
_REMEDIATION_ALLOWED_STATE_KEYS = {
    "snapshot_status",
    "observed_head_sha",
    "observed_base_sha",
    "budget_ok",
    "usage_available",
    "reason_code",
    "action_count",
    "result_head_sha",
    "health_baseline_id",
    "status",
}
_REMEDIATION_SENSITIVE_STATE_TOKENS = (
    "prompt",
    "handoff",
    "credential",
    "password",
    "secret",
    "authorization",
    "api_key",
    "private_key",
    "database",
    "failure",
    "usage",
    "token",
    "cost",
    "log",
)


def validate_remediation_state(state):
    """Return a bounded, redacted state mapping for remediation steps."""

    result = validate_human_request_state(state)

    def check_keys(value, *, top_level=False):
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = key.lower().replace("-", "_")
                if not (top_level and normalized in _REMEDIATION_ALLOWED_STATE_KEYS):
                    if any(
                        token in normalized
                        for token in _REMEDIATION_SENSITIVE_STATE_TOKENS
                    ):
                        raise ValueError(
                            "remediation transition state contains sensitive data"
                        )
                check_keys(item)
        elif isinstance(value, list):
            for item in value:
                check_keys(item)

    check_keys(result, top_level=True)
    return result


def validate_remediation_transition(transition):
    """Validate and copy the exact v1 remediation transition payload."""

    if not isinstance(transition, dict) or set(transition) != {
        "transition_id",
        "operation",
        "wait_seconds",
        "terminal",
        "state",
    }:
        raise ValueError("remediation transition has an invalid shape")
    transition_id = _required_identifier(transition["transition_id"], "transition_id")
    operation = transition["operation"]
    if not isinstance(operation, str) or operation not in _REMEDIATION_OPERATIONS:
        raise ValueError("remediation transition has an invalid operation")
    terminal = transition["terminal"]
    if type(terminal) is not bool:
        raise ValueError("remediation transition terminal must be a boolean")
    if operation == "terminal" and not terminal:
        raise ValueError("terminal operation must set terminal=true")
    if operation != "terminal" and terminal:
        raise ValueError("non-terminal operation must set terminal=false")
    wait_seconds = transition["wait_seconds"]
    if operation == "wait":
        if type(wait_seconds) is not int or not 1 <= wait_seconds <= 300:
            raise ValueError("wait transition seconds must be an integer from 1 to 300")
    elif wait_seconds is not None:
        raise ValueError("wait_seconds is only valid for wait transitions")
    state = validate_remediation_state(transition["state"])
    if operation == "validate_snapshot":
        snapshot_status = state.get("snapshot_status")
        if snapshot_status not in _REMEDIATION_SNAPSHOT_STATUSES:
            raise ValueError("snapshot validation requires an allowed snapshot_status")
        if snapshot_status == "current":
            _required_identifier(state.get("observed_head_sha"), "observed_head_sha")
            _required_identifier(state.get("observed_base_sha"), "observed_base_sha")
    elif operation == "enforce_budget":
        if type(state.get("budget_ok")) is not bool:
            raise ValueError("budget transition requires a boolean budget_ok")
        if type(state.get("usage_available")) is not bool:
            raise ValueError("budget transition requires a boolean usage_available")
        _required_identifier(state.get("reason_code"), "reason_code")
    elif operation == "audit":
        action_count = state.get("action_count")
        if type(action_count) is not int or action_count < 0:
            raise ValueError("audit transition requires a non-negative action_count")
    elif operation == "wait_health":
        _required_identifier(state.get("result_head_sha"), "result_head_sha")
        _required_identifier(state.get("health_baseline_id"), "health_baseline_id")
    elif operation == "terminal":
        status = state.get("status")
        if not isinstance(status, str) or status not in REMEDIATION_TERMINAL_STATUSES:
            raise ValueError("terminal transition requires an allowed status")
    return {
        "transition_id": transition_id,
        "operation": operation,
        "wait_seconds": wait_seconds,
        "terminal": terminal,
        "state": state,
    }


def validate_remediation_signal(episode_id, message):
    """Validate the WU5 ID-only signal contract for one remediation Episode."""

    if not isinstance(message, dict) or set(message) != _REMEDIATION_SIGNAL_KEYS:
        raise ValueError("remediation signal has an invalid shape")
    for key in _REMEDIATION_SIGNAL_KEYS:
        _required_identifier(message[key], key)
    if message["topic"] != "subject-events":
        raise ValueError("remediation signal topic is invalid")
    if (
        message["subject_type"] != "remediation_episode"
        or message["subject_id"] != episode_id
    ):
        raise ValueError("remediation signal subject conflicts with its workflow")
    return {key: message[key] for key in _REMEDIATION_SIGNAL_KEYS}


def validate_remediation_apply_result(transition, result):
    if not isinstance(result, dict) or set(result) != _REMEDIATION_APPLY_KEYS:
        raise ValueError("remediation apply result has an invalid shape")
    applied_id = _required_identifier(
        result["applied_transition_id"], "applied_transition_id"
    )
    if applied_id != transition["transition_id"]:
        raise ValueError("remediation apply result conflicts with its transition")
    if (
        not isinstance(result["status"], str)
        or result["status"] not in _REMEDIATION_APPLY_STATUSES
    ):
        raise ValueError("remediation apply result has an invalid status")
    return {
        "applied_transition_id": applied_id,
        "status": result["status"],
        "state": validate_remediation_state(result["state"]),
    }


def validate_remediation_consume_result(message, result):
    if not isinstance(result, dict) or set(result) != _REMEDIATION_CONSUME_KEYS:
        raise ValueError("remediation consume result has an invalid shape")
    signal_id = _required_identifier(result["signal_id"], "signal_id")
    if signal_id != message["signal_id"]:
        raise ValueError("remediation consume result conflicts with its signal")
    if (
        not isinstance(result["status"], str)
        or result["status"] not in _REMEDIATION_CONSUME_STATUSES
    ):
        raise ValueError("remediation consume result has an invalid status")
    return {
        "signal_id": signal_id,
        "status": result["status"],
        "state": validate_remediation_state(result["state"]),
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
                for token in ("credential", "password", "secret", "token", "private_key")
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
        if row[0] == episode["last_task_id"] or metadata.get("remediation_episode_id") == episode["episode_id"]:
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
    should use the ownership helpers below instead.
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
            binding = _safe_binding(workflow_store.get_binding(conn, "task", subject_id))
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
                        workflow_store.get_binding(conn, "remediation_episode", episode_id)
                    )
                    if episode_id
                    else None
                ),
                "workstream": _safe_row(workstream),
                "lifecycle": {
                    "workstream_id": task["workstream_id"],
                    "active_task_id": workstream["active_task_id"] if workstream else None,
                    "episode_id": episode_id,
                    **_task_relations(conn, subject_id),
                },
            }
            return snapshot

        episode = _load_episode(conn, subject_id)
        binding = _safe_binding(
            workflow_store.get_binding(conn, "remediation_episode", subject_id)
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
    binding = workflow_store.get_binding(conn, subject_type, subject_id)
    if not binding:
        return None
    if binding["owner_engine"] == "legacy" and binding["binding_status"] == "active":
        return binding
    return False


def _claim(conn, subject_type, subject_id, workflow_kind, eligible, now):
    existing = _existing_active_binding(conn, subject_type, subject_id)
    if existing is not None:
        return existing if existing is not False else None
    if not eligible:
        return None
    try:
        return workflow_store.ensure_legacy_binding(
            conn,
            subject_type,
            subject_id,
            workflow_kind,
            now,
        )
    except sqlite3.IntegrityError:
        # A concurrent DBOS claim wins; never transfer it to legacy.
        existing = workflow_store.get_binding(conn, subject_type, subject_id)
        if existing and existing["owner_engine"] == "legacy" and existing["binding_status"] == "active":
            return existing
        return None


def claim_legacy_subject(conn, subject_type, subject_id, *, now):
    """Atomically claim an eligible subject for the legacy engine.

    The transaction belongs to the caller. Existing DBOS and attention
    bindings are never changed and return ``None``. An already-active legacy
    binding may still be returned for a terminal domain row while crash
    recovery finishes; an unbound terminal row cannot be newly claimed.
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
    episode_status = episode["status"] if isinstance(episode, sqlite3.Row) else episode[7]
    return _claim(
        conn,
        "remediation_episode",
        subject_id,
        "pr_remediation",
        episode_status not in _TERMINAL_EPISODE_STATUSES,
        now,
    )


def require_subject_owner(conn, subject_type, subject_id, expected_owner_engine):
    """Return the active binding only when the expected engine owns it."""

    _validate_subject_type(subject_type)
    if expected_owner_engine not in {"legacy", "dbos"}:
        raise ValueError("unknown workflow owner engine")
    if subject_type == "task":
        task = _load_task(conn, subject_id)
        task_kind = task["task_kind"] if isinstance(task, sqlite3.Row) else task[2]
        if task_kind != "human_request":
            raise ValueError("workflow task subject is not an eligible human task")
    else:
        _load_episode(conn, subject_id)
    binding = workflow_store.get_binding(conn, subject_type, subject_id)
    if (
        not binding
        or binding["owner_engine"] != expected_owner_engine
        or binding["binding_status"] != "active"
    ):
        raise ValueError("workflow subject is not owned by the expected engine")
    return binding


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
                binding = workflow_store.get_binding(conn, subject_type, subject_id)
                if binding and binding["binding_status"] == "active":
                    workflow_store.mark_binding_attention(
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
        workflow_store.mark_binding_attention(
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
    binding = workflow_store.get_binding(conn, subject_type, subject_id)
    attempt = _attempt_dispatch_identity(conn, attempt_id)
    identity_status = _recovery_identity_status(evidence)
    logs = evidence.get("logs", {}) if isinstance(evidence, dict) else {}
    terminal_error = retry_policy.worker_error(logs)
    category = retry_policy.error_class(terminal_error)
    if category not in {"transient_failure", "rate_limited"}:
        _mark_recovery_attention(conn, binding, now)
        if binding:
            conn.execute("UPDATE workflow_bindings SET metadata_json=? WHERE subject_type=? AND subject_id=?",
                (json.dumps({**_json_object(binding.get("metadata_json")), "reason_code": category, "required_action": "Repair the reported cause, then retry the task."}), subject_type, subject_id))
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
    consistent_attempt = bool(
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
    ) if isinstance(evidence, dict) else False
    owner_ok = bool(
        binding
        and binding["binding_status"] == "active"
        and binding["owner_engine"] in {"legacy", "dbos"}
    )
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
        UPDATE workflow_bindings
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
    binding = workflow_store.get_binding(conn, subject_type, subject_id)
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
        workflow_store.mark_binding_attention(conn, subject_type, subject_id, now)
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
    """Apply one classified fact to an existing binding in the caller transaction.

    The operation records only the bounded evidence projection.  DBOS-owned
    subjects receive one ID-only outbox signal before terminal/Attention state
    is persisted; legacy subjects retain their local path and receive no
    signal.
    """

    from robert_agent import workflow_events, workflow_signals

    binding = workflow_store.get_binding(conn, subject_type, subject_id)
    if not binding:
        raise ValueError("workflow subject is not bound")
    classification, evidence = workflow_events.classify_and_normalize(fact)
    if (
        evidence["subject_type"] != subject_type
        or evidence["subject_id"] != subject_id
    ):
        raise ValueError("workflow fact subject does not match its binding")
    metadata = _json_object(binding.get("metadata_json"))
    latest = metadata.get("latest_fact")
    if binding["owner_engine"] == "dbos":
        recorded = conn.execute(
            """
            SELECT signal_id, payload_json
            FROM workflow_outbox
            WHERE workflow_id = ? AND topic = ? AND dedupe_key = ?
            """,
            (binding["workflow_id"], "subject-events", evidence["fact_id"]),
        ).fetchone()
        if recorded:
            return {
                "classification": classification,
                "fact_id": evidence["fact_id"],
                "evidence": latest if isinstance(latest, dict) else evidence,
                "duplicate": True,
                "binding_status": binding["binding_status"],
                "signal_id": recorded[0],
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

    metadata["latest_fact"] = evidence
    conn.execute(
        "UPDATE workflow_bindings SET metadata_json = ?, updated_at = ? WHERE subject_type = ? AND subject_id = ?",
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True), now, subject_type, subject_id),
    )

    signal_id = None
    if binding["owner_engine"] == "dbos" and classification in {
        workflow_events.ADDITIVE,
        workflow_events.INVALIDATING,
        workflow_events.TERMINAL,
    }:
        signal = workflow_signals.record_subject_signal(
            conn,
            {**binding, "metadata_json": json.dumps(metadata, sort_keys=True)},
            workflow_signals.DBOS_TOPIC,
            evidence["fact_id"],
            {"fact_id": evidence["fact_id"]},
            now,
        )
        signal_id = signal["signal_id"]

    if classification == workflow_events.TERMINAL:
        workflow_store.close_binding(conn, subject_type, subject_id, now)
    elif classification == workflow_events.INVALIDATING:
        workflow_store.mark_binding_attention(conn, subject_type, subject_id, now)

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


def apply_subject_fact(conn, subject_type, subject_id, fact, *, now):
    """Compatibility spelling for the caller-transactional fact operation."""

    return apply_classified_fact(conn, subject_type, subject_id, fact, now=now)
