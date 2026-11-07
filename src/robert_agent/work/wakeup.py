"""Durable local wakeup hints for the DD GitHub agent."""

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import sqlite_master as db_read_sqlite_master
from robert_agent.persistence.queries import wakeups as db_read_wakeups
from robert_agent.persistence.stores import wakeups as db_write_wakeups

from datetime import datetime, timezone
import json
from uuid import uuid4


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


def _logical_reason(reason, metadata):
    # The stored enum remains compatible with older databases. Expose machine
    # wakeups by their actual purpose while preserving their ID and dedupe key.
    kind = metadata.get("kind") if isinstance(metadata, dict) else None
    if reason == "manual_operator_request" and kind in {
        "github_observation_ready",
        "github_context_refresh",
    }:
        return kind
    return reason


def _wakeup_from_row(row):
    if row is None:
        return None
    if hasattr(row, "keys"):
        payload = dict(row)
    else:
        columns = [
            "wakeup_id",
            "repo_id",
            "reason",
            "dedupe_key",
            "work_item_id",
            "task_id",
            "attempt_id",
            "result_id",
            "source_run_id",
            "consumed_run_id",
            "status",
            "not_before_at",
            "expires_at",
            "attempt_count",
            "created_at",
            "updated_at",
            "metadata_json",
        ]
        payload = {column: row[index] for index, column in enumerate(columns)}
    payload["metadata"] = _decode_json(payload.pop("metadata_json"), {}) or {}
    payload["reason"] = _logical_reason(payload["reason"], payload["metadata"])
    return payload


def request_wakeup(
    conn,
    *,
    repo_id,
    reason,
    dedupe_key,
    work_item_id=None,
    task_id=None,
    attempt_id=None,
    result_id=None,
    source_run_id=None,
    metadata=None,
    not_before_at=None,
    expires_at=None,
    now=None,
):
    now = now or utc_now()
    due_at = not_before_at or now
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
    wakeup_id = f"wakeup-{uuid4().hex[:12]}"
    existing = db_records.first(db_read_wakeups.find_for_request_wakeup(conn, repo_id, reason, dedupe_key))
    if existing:
        payload = _wakeup_from_row(existing)
        if payload["status"] == "pending":
            payload["status"] = "already_pending"
        return payload
    db_write_wakeups.insert_for_request_wakeup(
        conn,
        wakeup_id,
        repo_id,
        reason,
        dedupe_key,
        work_item_id,
        task_id,
        attempt_id,
        result_id,
        source_run_id,
        due_at,
        expires_at,
        now,
        now,
        metadata_json,
    )
    return {
        "wakeup_id": wakeup_id,
        "repo_id": repo_id,
        "reason": _logical_reason(reason, metadata),
        "dedupe_key": dedupe_key,
        "work_item_id": work_item_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "result_id": result_id,
        "source_run_id": source_run_id,
        "consumed_run_id": None,
        "status": "pending",
        "not_before_at": due_at,
        "expires_at": expires_at,
        "attempt_count": 0,
        "created_at": now,
        "updated_at": now,
        "metadata": metadata or {},
    }


def list_wakeups(conn, *, status=None, limit=20, now=None):
    if not db_read_sqlite_master.table_exists(conn, "wakeups"):
        return []
    return [_wakeup_from_row(row) for row in db_read_wakeups.list_wakeups(conn, status, limit, now)]


def summarize_wakeups(conn, now=None):
    if not db_read_sqlite_master.table_exists(conn, "wakeups"):
        return {
            "available": False,
            "total": 0,
            "pending": 0,
            "due": 0,
            "by_status": {},
            "by_reason": {},
        }
    now = now or utc_now()
    by_status = {row[0]: row[1] for row in db_read_wakeups.find_for_summarize_wakeups(conn)}
    by_reason = {row[0]: row[1] for row in db_read_wakeups.find_for_summarize_wakeups_2(conn)}
    due = db_records.first(db_read_wakeups.find_for_summarize_wakeups_3(conn, now, now))[0]
    total = sum(by_status.values())
    return {
        "available": True,
        "total": total,
        "pending": by_status.get("pending", 0),
        "due": due,
        "by_status": dict(sorted(by_status.items())),
        "by_reason": dict(sorted(by_reason.items())),
    }


def consume_wakeups_for_results(conn, *, result_ids, run_id, now=None):
    if not result_ids or not db_read_sqlite_master.table_exists(conn, "wakeups"):
        return 0
    now = now or utc_now()
    cur = db_write_wakeups.consume_wakeups_for_results_filtered(conn, result_ids, run_id, now)
    return cur


def expire_due_wakeups(conn, *, now=None):
    if not db_read_sqlite_master.table_exists(conn, "wakeups"):
        return 0
    now = now or utc_now()
    cur = db_write_wakeups.update_for_expire_due_wakeups(conn, now, now)
    return cur


def has_due_context_refresh(conn, repo_ids, now):
    return bool(db_records.first(db_read_wakeups.has_due_context_refresh_filtered(conn, repo_ids, now, now)))
