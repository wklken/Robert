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
