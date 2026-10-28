"""Bounded health measurements that persist deduplicated delay notices."""

import json
import os
from pathlib import Path
from contextlib import closing
import sqlite3


def _control_failure_snapshot(conn, limit):
    """Track separate control lanes so a healthy drain cannot hide a dead scan."""
    streaks, recovered = {}, set()
    for lane in ('scan', 'local', 'publish', 'context_refresh'):
        events = conn.execute(
            """SELECT daemon_event_id,status FROM daemon_events
            WHERE ((status='failed' AND (json_type(metadata_json,'$.control_lane')='text'
                    OR event_type IN ('child_failed','child_timeout','executor_interrupted',
                                      'partial_failure','repository_pipeline_failed')))
                OR event_type IN ('live_poll_completed','local_drain_completed',
                                  'scan_completed','publish_completed'))
              AND COALESCE(json_extract(metadata_json,'$.control_lane'),
                  CASE WHEN json_extract(metadata_json,'$.context_refresh')=1 THEN 'context_refresh'
                       WHEN event_type='local_drain_completed' THEN 'local'
                       WHEN event_type='publish_completed' THEN 'publish' ELSE 'scan' END)=?
            ORDER BY created_at DESC,daemon_event_id DESC LIMIT ?""", (lane, limit),
        ).fetchall()
        for identifier, status in events:
            if status != 'failed':
                if not streaks.get(lane):
                    recovered.add(lane)
                break
            streaks.setdefault(lane, []).append(identifier)
    # Abandoned executors are reconciled in agent_runs, not necessarily in a
    # daemon event. Their successful successor ends that failure streak.
    runs = conn.execute(
        """SELECT run_id,status,error_json FROM agent_runs
        WHERE status IN ('completed','failed')
        ORDER BY COALESCE(finished_at,started_at) DESC,run_id DESC LIMIT ?""", (limit,),
    ).fetchall()
    for identifier, status, encoded in runs:
        if status != 'failed' or json.loads(encoded or '{}').get('status') != 'executor_interrupted':
            if not streaks.get('executor'):
                recovered.add('executor')
            break
        streaks.setdefault('executor', []).append(identifier)
    return streaks, recovered


def _persist_control_failure_alerts(conn, now, streaks, recovered):
    for lane in {'scan', 'local', 'publish', 'context_refresh', 'executor'}:
        failures = streaks.get(lane, [])
        existing = conn.execute(
            """SELECT notification_id FROM notifications
            WHERE notification_type='control_plane_failed' AND status!='resolved'
              AND json_extract(metadata_json,'$.control_lane')=?""", (lane,),
        ).fetchall()
        if not failures and lane in recovered:
            for (identifier,) in existing:
                conn.execute("UPDATE notifications SET status='resolved' WHERE notification_id=?", (identifier,))
        elif len(failures) >= 3 and not existing:
            conn.execute(
                """INSERT OR IGNORE INTO notifications(notification_id,notification_type,
                    channel,status,created_at,metadata_json)
                VALUES (?,'control_plane_failed','local','recorded',?,?)""",
                ('notification-control-' + lane + '-' + failures[-1], now, json.dumps({
                    'control_lane': lane, 'failure_count': len(failures),
                    'reason_code': 'control_plane_failed',
                    'required_action': 'Inspect repeated control failures and restore GitHub event processing.',
                })),
            )


def collect(db_path, *, now, repo_ids, limit=100):
    if not repo_ids:
        return {}
    placeholders = ",".join("?" for _ in repo_ids)
    with closing(sqlite3.connect(db_path, timeout=0.1)) as conn:
        streaks, recovered = _control_failure_snapshot(conn, limit)
        control_failure_streak = max((len(failures) for failures in streaks.values()), default=0)
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        delayed = conn.execute(
            f"""SELECT t.task_id,
            CASE WHEN EXISTS (SELECT 1 FROM github_actions ga WHERE ga.task_id=t.task_id AND ga.audit_status='accepted' AND ga.publish_status='not_published')
            THEN 'publication_delayed' ELSE 'queue_delayed' END
            FROM tasks t JOIN workstreams w ON w.workstream_id=t.workstream_id
            WHERE w.repo_id IN ({placeholders}) AND t.lifecycle IN ('queued','running')
            AND ((t.lifecycle='queued' AND julianday(t.created_at)<julianday(?)-5.0/1440)
                OR EXISTS (SELECT 1 FROM github_actions ga WHERE ga.task_id=t.task_id AND ga.audit_status='accepted' AND ga.publish_status='not_published' AND julianday(ga.created_at)<julianday(?)-2.0/1440))
            ORDER BY t.created_at LIMIT ?""",
            [*repo_ids, now, now, limit],
        ).fetchall()
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
        failed_notifications = conn.execute("SELECT count(*) FROM notifications WHERE json_extract(metadata_json,'$.delivery.status')='exhausted'").fetchone()[0]
        oldest_notification = conn.execute("SELECT MAX(0, (julianday(?)-MIN(julianday(created_at)))*86400) FROM notifications WHERE channel='local' AND status NOT IN ('sent','resolved') AND COALESCE(json_extract(metadata_json,'$.delivery.status'),'') NOT IN ('sent','exhausted')", (now,)).fetchone()[0]
        outbox = conn.execute("SELECT count(*), MAX(0, (julianday(?)-MIN(julianday(created_at)))*86400) FROM workflow_outbox WHERE status='pending'", (now,)).fetchone()
        outbox_attention = conn.execute("SELECT count(*) FROM workflow_outbox WHERE status='attention'").fetchone()[0]
    alerts_persisted = True
    try:
        with closing(sqlite3.connect(db_path, timeout=0.1)) as conn, conn:
            _persist_control_failure_alerts(conn, now, streaks, recovered)
            for task_id, reason in delayed:
                conn.execute(
                    """INSERT OR IGNORE INTO notifications(notification_id,task_id,notification_type,channel,status,created_at,metadata_json)
                    VALUES (?,?,'execution_delayed','local','recorded',?,?)""",
                    (
                        "notification-delay-" + task_id + "-" + reason,
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
    except sqlite3.OperationalError as exc:
        if str(exc) not in {'database is locked', 'database table is locked', 'database schema is locked'}:
            raise
        alerts_persisted = False
    wal = Path(str(db_path) + "-wal")
    return {
        "control_failure_streak": control_failure_streak,
        "alerts_persisted": alerts_persisted,
        "database_bytes": pages * page_size,
        "reusable_bytes": free * page_size,
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
        "delayed_tasks_sample": len(delayed),
        "stale_runs_sample": orphaned,
        "pending_notifications_sample": unsent,
        "sample_limit": limit,
        "delivery_scope": "database",
        "failed_notifications": failed_notifications,
        "oldest_notification_seconds": oldest_notification,
        "pending_outbox": outbox[0],
        "oldest_outbox_seconds": outbox[1],
        "attention_outbox": outbox_attention,
        "notification_delivery_configured": bool(
            os.environ.get("ROBERT_NOTIFICATION_COMMAND")
        ),
    }
