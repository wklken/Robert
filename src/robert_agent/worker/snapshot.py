#!/usr/bin/env python3
import argparse
from contextlib import closing
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from robert_agent.common import emit


VALID_PHASES = {"prepare", "analyze", "plan", "execute", "verify", "publish", "handoff"}


def build_snapshot(task_id, attempt_id, phase, status, summary, next_step=""):
    if phase not in VALID_PHASES:
        raise ValueError(f"invalid worker phase: {phase}")
    return {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "phase": phase,
        "status": status,
        "summary": summary,
        "next_step": next_step,
    }


def record_snapshot(db_path, task_id, attempt_id, phase, status, summary, next_step=""):
    row = build_snapshot(task_id, attempt_id, phase, status, summary, next_step)
    created_at = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            """
            INSERT INTO worker_phases(
              phase_id, attempt_id, phase, status, summary, next_step, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"phase-{uuid4().hex[:12]}",
                attempt_id,
                phase,
                status,
                summary,
                next_step,
                created_at,
            ),
        )
        updated = conn.execute(
            "UPDATE attempts SET heartbeat_at = ? WHERE attempt_id = ?",
            (created_at, attempt_id),
        ).rowcount
        if updated != 1:
            raise ValueError("attempt_id does not exist")
        matched = conn.execute(
            "SELECT 1 FROM attempts WHERE attempt_id = ? AND task_id = ?",
            (attempt_id, task_id),
        ).fetchone()
        if not matched:
            raise ValueError("attempt_id does not belong to task_id")
    return {"ok": True, "status": "recorded", "snapshot": row}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--next-step", default="")
    args = parser.parse_args(argv)
    if args.db:
        result = record_snapshot(
            db_path=args.db,
            task_id=args.task_id,
            attempt_id=args.attempt_id,
            phase=args.phase,
            status=args.status,
            summary=args.summary,
            next_step=args.next_step,
        )
    else:
        result = {"ok": True, "status": "built", "snapshot": build_snapshot(
            task_id=args.task_id,
            attempt_id=args.attempt_id,
            phase=args.phase,
            status=args.status,
            summary=args.summary,
            next_step=args.next_step,
        )}
    return emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
