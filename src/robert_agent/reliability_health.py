"""Bounded, read-only health measurements and deduplicated delay notices."""

import json
import os
from pathlib import Path
from contextlib import closing
import sqlite3


def collect(db_path, *, now, repo_ids, limit=100):
    if not repo_ids:
        return {}
    placeholders = ",".join("?" for _ in repo_ids)
    with closing(sqlite3.connect(db_path, timeout=0.1)) as conn, conn:
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        delayed = conn.execute(
            f"""SELECT t.task_id,
            CASE WHEN EXISTS (SELECT 1 FROM github_actions ga WHERE ga.task_id=t.task_id AND ga.audit_status='accepted' AND ga.publish_status='not_published')
            THEN 'publication_delayed' ELSE 'queue_delayed' END
            FROM tasks t JOIN workstreams w ON w.workstream_id=t.workstream_id
            WHERE w.repo_id IN ({placeholders}) AND t.lifecycle IN ('queued','running')
            AND ((t.lifecycle='queued' AND julianday(t.created_at)<julianday(?)-10.0/1440)
                OR EXISTS (SELECT 1 FROM github_actions ga WHERE ga.task_id=t.task_id AND ga.audit_status='accepted' AND ga.publish_status='not_published' AND julianday(ga.created_at)<julianday(?)-10.0/1440))
            ORDER BY t.created_at LIMIT ?""",
            [*repo_ids, now, now, limit],
        ).fetchall()
        for task_id, reason in delayed:
            conn.execute(
                """INSERT OR IGNORE INTO notifications(notification_id,task_id,notification_type,channel,status,created_at,metadata_json)
                VALUES (?,?,'execution_delayed','local','recorded',?,?)""",
                (
                    "notification-delay-" + task_id,
                    task_id,
                    now,
                    json.dumps(
                        {
                            "reason_code": reason,
                            "required_action": "Inspect queue capacity, worker state and publication failure details.",
                        }
                    ),
                ),
            )
        orphaned = conn.execute(
            """SELECT count(*) FROM (SELECT run_id FROM agent_runs ar
            WHERE status='running' AND julianday(started_at)<julianday(?)-1.0/24
            AND NOT EXISTS (SELECT 1 FROM leases l WHERE l.owner_id=ar.run_id AND l.status='active' AND julianday(l.expires_at)>julianday(?)) LIMIT ?)""",
            (now, now, limit),
        ).fetchone()[0]
        unsent = conn.execute(
            "SELECT count(*) FROM (SELECT notification_id FROM notifications WHERE status='recorded' AND COALESCE(json_extract(metadata_json,'$.delivery.status'),'') NOT IN ('sent','exhausted') LIMIT ?)",
            (limit,),
        ).fetchone()[0]
    wal = Path(str(db_path) + "-wal")
    return {
        "database_bytes": pages * page_size,
        "reusable_bytes": free * page_size,
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
        "delayed_tasks_sample": len(delayed),
        "stale_runs_sample": orphaned,
        "pending_notifications_sample": unsent,
        "sample_limit": limit,
        "notification_delivery_configured": bool(
            os.environ.get("ROBERT_NOTIFICATION_COMMAND")
        ),
    }
