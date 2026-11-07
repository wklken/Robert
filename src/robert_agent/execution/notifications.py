"""Persist execution notices without external notification effects."""

import json
from uuid import uuid4


def insert(conn, notification_type, status, run_now, metadata=None):
    metadata = metadata or {}
    conn.execute(
        """
        INSERT INTO notifications(
          notification_id, task_id, notification_type, channel, status, created_at, metadata_json
        )
        VALUES (?, ?, ?, 'local', ?, ?, ?)
        """,
        (
            f"notification-{uuid4().hex[:12]}",
            metadata.get("task_id"),
            notification_type,
            status,
            run_now,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        ),
    )


def update_failure(conn, kind, key, run_now, metadata=None):
    """Keep one notice per unresolved source/repository failure episode."""
    predicate = """notification_type=? AND status!='resolved'
        AND COALESCE(json_extract(metadata_json,'$.failure_key'),
                     json_extract(metadata_json,'$.source_key'),
                     json_extract(metadata_json,'$.repo_id'))=?"""
    if metadata is None:
        conn.execute(f"UPDATE notifications SET status='resolved' WHERE {predicate}", (kind, key))
    elif not conn.execute(f"SELECT 1 FROM notifications WHERE {predicate} LIMIT 1", (kind, key)).fetchone():
        insert(conn, kind, "recorded", run_now, {**metadata, "failure_key": key})


def resolve_control_failure(conn, lane):
    conn.execute(
        """UPDATE notifications SET status='resolved'
        WHERE notification_type='control_plane_failed' AND status!='resolved'
        AND json_extract(metadata_json,'$.control_lane')=?""",
        (lane,),
    )


def record_control_stop(conn, daemon_run_id, lane, now, attention):
    conn.execute(
        """INSERT OR IGNORE INTO notifications
        (notification_id,notification_type,channel,status,created_at,metadata_json)
        VALUES (?,'control_plane_failed','local','recorded',?,?)""",
        (
            "notification-control-stop-" + daemon_run_id + "-" + lane,
            now,
            json.dumps(attention),
        ),
    )


def has_pending_local_delivery(conn, repo_ids, now):
    placeholders = ",".join("?" for _ in repo_ids)
    return bool(
        conn.execute(
            f"""SELECT 1 FROM notifications
        WHERE channel='local' AND status NOT IN ('resolved','sent')
          AND COALESCE(json_extract(metadata_json,'$.delivery.status'),'') NOT IN ('sent','exhausted')
          AND (json_extract(metadata_json,'$.delivery.next_retry_at') IS NULL
               OR julianday(json_extract(metadata_json,'$.delivery.next_retry_at'))<=julianday(?))
          AND (task_id IS NULL OR task_id IN (
            SELECT t.task_id FROM tasks t JOIN workstreams w ON w.workstream_id=t.workstream_id
            WHERE w.repo_id IN ({placeholders}))) LIMIT 1""",
            [now, *repo_ids],
        ).fetchone()
    )
