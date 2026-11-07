from robert_agent.persistence.queries import notifications as db_read_notifications

from robert_agent.persistence import database as db_database

from robert_agent.persistence import records as db_records
from robert_agent.persistence.stores import notifications as db_write_notifications

#!/usr/bin/env python3
import argparse
from contextlib import closing
import json
import hashlib
import os
import shlex
import subprocess
import time
from datetime import datetime, timezone
from uuid import uuid4

from robert_agent.foundation.commands import emit
from robert_agent.execution import retry as retry_policy
from robert_agent.security import redaction
from robert_agent.delivery.lock import task_publication_lock


def record_notification(db_path, notification_type, status, metadata=None, channel="local"):
    metadata = metadata or {}
    with closing(db_database.connect(db_path)) as conn, conn:
        db_write_notifications.record_notification(
            conn,
            f"notification-{uuid4().hex[:12]}",
            metadata.get("task_id"),
            notification_type,
            channel,
            status,
            datetime.now(timezone.utc).isoformat(),
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        )
    return {"ok": True, "status": status, "notification_type": notification_type}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--type", required=True)
    parser.add_argument("--status", default="recorded")
    parser.add_argument("--metadata", default="{}")
    args = parser.parse_args(argv)
    return emit(
        record_notification(
            db_path=args.db,
            notification_type=args.type,
            status=args.status,
            metadata=json.loads(args.metadata),
        )
    )


def deliver_pending(db_path, *, command=None, runner=subprocess.run, now=None, limit=100, repo_ids=None):
    """At-least-once operator delivery; receiver dedupes the stable delivery_id.

    No command is run unless configured. Secrets and command arguments are never
    included in the outbound payload or persisted error.
    """
    command = command or os.environ.get("ROBERT_NOTIFICATION_COMMAND")
    if not command:
        return {"sent": 0, "failed": 0, "status": "not_configured"}
    argv = shlex.split(command) if isinstance(command, str) else list(command)
    now = now or datetime.now(timezone.utc).isoformat()
    counts = {"sent": 0, "failed": 0, "status": "processed"}
    with closing(db_database.connect(db_path)) as conn:
        ids = [r[0] for r in db_read_notifications.find_for_deliver_pending(conn, now, limit, repo_ids)]
    processed = 0
    deadline = time.monotonic() + 10
    for notification_id in ids:
        if processed >= limit or time.monotonic() >= deadline:
            break
        with task_publication_lock(db_path, "notify-" + notification_id) as acquired:
            if not acquired:
                continue
            with closing(db_database.connect(db_path)) as conn:
                row = db_records.first(
                    db_read_notifications.find_task_id_and_notification_type_and_status_and_metadata_json_by_notification_id(
                        conn, notification_id
                    )
                )
            if not row or row[2] == "resolved":
                continue
            metadata = json.loads(row[3] or "{}")
            state = {
                key: metadata[key]
                for key in ("reason_code", "error_class", "required_action", "retryable")
                if key in metadata
            }
            digest = hashlib.sha256(json.dumps([row[1], state], sort_keys=True).encode()).hexdigest()[:16]
            delivery_id = notification_id + ":" + digest
            previous = metadata.get("delivery", {})
            if previous.get("delivery_id") != delivery_id:
                previous = {}
            if previous.get("status") in {"sent", "exhausted"} or (
                previous.get("next_retry_at")
                and retry_policy.timestamp(previous["next_retry_at"]) > retry_policy.timestamp(now)
            ):
                continue
            if int(previous.get("attempt_count", 0)) >= retry_policy.MAX_ATTEMPTS or (
                previous.get("first_failed_at")
                and (retry_policy.timestamp(now) - retry_policy.timestamp(previous["first_failed_at"])).total_seconds()
                >= retry_policy.MAX_ELAPSED_SECONDS
            ):
                previous.update(status="exhausted", retryable=False, next_retry_at=None)
                metadata["delivery"] = previous
                with closing(db_database.connect(db_path)) as conn, conn:
                    db_write_notifications.set_metadata_json_by_notification_id(
                        conn, json.dumps(metadata), notification_id
                    )
                continue
            safe = redaction.redact_text(
                json.dumps(
                    {
                        "task_id": row[0],
                        "type": row[1],
                        **state,
                        "safe_error": str(metadata.get("safe_error", ""))[:2000],
                        "delivery_id": delivery_id,
                    },
                    ensure_ascii=False,
                )
            )
            # High-risk fields are omitted rather than delivered as raw text.
            payload = (
                safe["text"]
                if safe["ok"]
                else json.dumps(
                    {
                        "task_id": row[0],
                        "type": row[1],
                        "delivery_id": delivery_id,
                        "reason": "Details require local inspection.",
                    }
                )
            )
            processed += 1
            state = retry_policy.failure_state(previous, "delivery unavailable", now)
            state.update(delivery_id=delivery_id, status="sending")
            with closing(db_database.connect(db_path)) as conn, conn:
                fresh = json.loads(
                    db_records.first(
                        db_read_notifications.find_metadata_json_by_notification_id(conn, notification_id)
                    )[0]
                    or "{}"
                )
                fresh["delivery"] = state
                db_write_notifications.set_metadata_json_by_notification_id(conn, json.dumps(fresh), notification_id)
            try:
                sent = (
                    runner(argv, input=payload, text=True, capture_output=True, timeout=5, check=False).returncode == 0
                )
            except (OSError, subprocess.SubprocessError):
                sent = False
            state.update(
                status="sent" if sent else ("failed" if state["retryable"] else "exhausted"),
                sent_at=now if sent else None,
            )
            with closing(db_database.connect(db_path)) as conn, conn:
                fresh = json.loads(
                    db_records.first(
                        db_read_notifications.find_metadata_json_by_notification_id(conn, notification_id)
                    )[0]
                    or "{}"
                )
                fresh["delivery"] = state
                db_write_notifications.set_metadata_json_by_notification_id(conn, json.dumps(fresh), notification_id)
            counts["sent" if sent else "failed"] += 1
    return counts


if __name__ == "__main__":
    raise SystemExit(main())
