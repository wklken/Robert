from robert_agent.persistence import database as db_database
from robert_agent.persistence import transactions as db_transactions
from robert_agent.persistence.dialects import sqlite as db_sqlite

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import attempts as db_read_attempts
from robert_agent.persistence.queries import worker_phases as db_read_worker_phases
from robert_agent.persistence.stores import attempts as db_write_attempts
from robert_agent.persistence.stores import worker_phases as db_write_worker_phases

#!/usr/bin/env python3
from contextlib import closing
from datetime import datetime, timezone
from uuid import uuid4


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
    with closing(db_database.connect(db_path)) as conn, conn:
        db_sqlite.configure(conn, foreign_keys=True)
        db_sqlite.configure(conn, busy_timeout_ms=5000)
        db_transactions.begin(conn, immediate=True)
        attempt = db_records.first(db_read_attempts.find_task_id_by_attempt_id(conn, attempt_id))
        if not attempt:
            raise ValueError("attempt_id does not exist")
        if attempt[0] != task_id:
            raise ValueError("attempt_id does not belong to task_id")
        created_at = datetime.now(timezone.utc).isoformat()
        previous = db_records.first(
            db_read_worker_phases.find_phase_and_status_and_summary_and_next_step_by_attempt_id(conn, attempt_id)
        )
        # Repeated liveness samples carry no new phase or command transition.
        # Serialize the comparison and insert with the heartbeat update.
        if previous != (phase, status, summary, next_step):
            db_write_worker_phases.record_snapshot(
                conn, f"phase-{uuid4().hex[:12]}", attempt_id, phase, status, summary, next_step, created_at
            )
        db_write_attempts.record_snapshot(conn, created_at, attempt_id, task_id)
    return {"ok": True, "status": "recorded", "snapshot": row}
