"""Durable local wakeup hints for the DD GitHub agent."""

from datetime import datetime, timezone
import json
from uuid import uuid4


WAKEUP_STATUSES = ("pending", "consumed", "expired", "canceled")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _table_exists(conn):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'wakeups'"
    ).fetchone()
    return bool(row)


def _decode_json(value, default):
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return default
    return parsed


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
    existing = conn.execute(
        """
        SELECT wakeup_id, repo_id, reason, dedupe_key, work_item_id, task_id, attempt_id,
               result_id, source_run_id, consumed_run_id, status, not_before_at,
               expires_at, attempt_count, created_at, updated_at, metadata_json
        FROM wakeups
        WHERE repo_id = ?
          AND reason = ?
          AND dedupe_key = ?
        """,
        (repo_id, reason, dedupe_key),
    ).fetchone()
    if existing:
        payload = _wakeup_from_row(existing)
        if payload["status"] == "pending":
            payload["status"] = "already_pending"
        return payload
    conn.execute(
        """
        INSERT INTO wakeups(
          wakeup_id, repo_id, reason, dedupe_key, work_item_id, task_id, attempt_id,
          result_id, source_run_id, status, not_before_at, expires_at,
          created_at, updated_at, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
        """,
        (
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
        ),
    )
    return {
        "wakeup_id": wakeup_id,
        "repo_id": repo_id,
        "reason": reason,
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
    if not _table_exists(conn):
        return []
    clauses = []
    params = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if now:
        clauses.append("(expires_at IS NULL OR expires_at > ?)")
        params.append(now)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT wakeup_id, repo_id, reason, dedupe_key, work_item_id, task_id, attempt_id,
               result_id, source_run_id, consumed_run_id, status, not_before_at,
               expires_at, attempt_count, created_at, updated_at, metadata_json
        FROM wakeups
        {where}
        ORDER BY
          CASE status
            WHEN 'pending' THEN 0
            WHEN 'expired' THEN 1
            WHEN 'consumed' THEN 2
            ELSE 3
          END,
          not_before_at,
          created_at,
          wakeup_id
        LIMIT ?
        """,
        params + [limit],
    ).fetchall()
    return [_wakeup_from_row(row) for row in rows]


def summarize_wakeups(conn, now=None):
    if not _table_exists(conn):
        return {
            "available": False,
            "total": 0,
            "pending": 0,
            "due": 0,
            "by_status": {},
            "by_reason": {},
        }
    now = now or utc_now()
    by_status = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT status, COUNT(*) FROM wakeups GROUP BY status"
        )
    }
    by_reason = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT reason, COUNT(*) FROM wakeups GROUP BY reason"
        )
    }
    due = conn.execute(
        """
        SELECT COUNT(*)
        FROM wakeups
        WHERE status = 'pending'
          AND not_before_at <= ?
          AND (expires_at IS NULL OR expires_at > ?)
        """,
        (now, now),
    ).fetchone()[0]
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
    if not result_ids or not _table_exists(conn):
        return 0
    now = now or utc_now()
    placeholders = ",".join("?" for _result_id in result_ids)
    cur = conn.execute(
        f"""
        UPDATE wakeups
        SET status = 'consumed',
            consumed_run_id = ?,
            updated_at = ?
        WHERE reason = 'worker_result_ready'
          AND status = 'pending'
          AND result_id IN ({placeholders})
        """,
        [run_id, now] + list(result_ids),
    )
    return cur.rowcount


def expire_due_wakeups(conn, *, now=None):
    if not _table_exists(conn):
        return 0
    now = now or utc_now()
    cur = conn.execute(
        """
        UPDATE wakeups
        SET status = 'expired',
            updated_at = ?
        WHERE status = 'pending'
          AND expires_at IS NOT NULL
          AND expires_at <= ?
        """,
        (now, now),
    )
    return cur.rowcount
