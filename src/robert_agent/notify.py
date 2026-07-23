#!/usr/bin/env python3
import argparse
from contextlib import closing
import json
import shlex
import sqlite3
import subprocess
from datetime import datetime, timezone
from uuid import uuid4

from robert_agent.common import emit


def record_notification(db_path, notification_type, status, metadata=None, channel="local"):
    metadata = metadata or {}
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            """
            INSERT INTO notifications(
              notification_id, task_id, notification_type, channel, status, created_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"notification-{uuid4().hex[:12]}",
                metadata.get("task_id"),
                notification_type,
                channel,
                status,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            ),
        )
    return {"ok": True, "status": status, "notification_type": notification_type}


def send_notification(db_path, notification, command=None, timeout_seconds=5):
    status = "recorded"
    if command:
        proc = subprocess.run(
            shlex.split(command),
            input=json.dumps(notification, ensure_ascii=False),
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        status = "sent" if proc.returncode == 0 else "failed"
    return record_notification(
        db_path=db_path,
        notification_type=notification["type"],
        status=status,
        metadata=notification,
    )


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


if __name__ == "__main__":
    raise SystemExit(main())
