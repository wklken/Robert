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
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("BEGIN IMMEDIATE")
        attempt = conn.execute(
            "SELECT task_id FROM attempts WHERE attempt_id = ?", (attempt_id,),
        ).fetchone()
        if not attempt:
            raise ValueError("attempt_id does not exist")
        if attempt[0] != task_id:
            raise ValueError("attempt_id does not belong to task_id")
        created_at = datetime.now(timezone.utc).isoformat()
        previous = conn.execute(
            """SELECT phase, status, summary, next_step FROM worker_phases
            WHERE attempt_id = ? ORDER BY created_at DESC, phase_id DESC LIMIT 1""",
            (attempt_id,),
        ).fetchone()
        # Repeated liveness samples carry no new phase or command transition.
        # Serialize the comparison and insert with the heartbeat update.
        if previous != (phase, status, summary, next_step):
            conn.execute(
                """INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (f"phase-{uuid4().hex[:12]}", attempt_id, phase, status,
                 summary, next_step, created_at),
            )
        conn.execute(
            "UPDATE attempts SET heartbeat_at = ? WHERE attempt_id = ? AND task_id = ?",
            (created_at, attempt_id, task_id),
        )
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
