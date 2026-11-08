"""Durable file handoffs for native PR review rounds (SQLite, no agent framework)."""

from robert_agent.foundation.contracts.execution import ModelRunStatus
from robert_agent.persistence.rounds import records as round_records

from robert_agent.persistence import database as db_database
from robert_agent.persistence.dialects import sqlite as db_sqlite

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import review_runs as db_read_review_runs
from robert_agent.persistence.queries import sqlite_master as db_read_sqlite_master
from robert_agent.persistence.stores import (
    review_process_pool as db_write_review_process_pool,
)
from robert_agent.persistence.stores import review_runs as db_write_review_runs
from robert_agent.persistence.stores import review_sessions as db_write_review_sessions
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(db_path):
    with closing(db_database.connect(db_path, timeout=10)) as conn, conn:
        db_sqlite.configure(conn, foreign_keys=True)
        yield conn


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def decode(row):
    if row is None:
        return None
    result = dict(row)
    for field in ("request_json", "manifest_json", "dispatch_json", "artifact_json"):
        if field in result:
            result[field[:-5]] = json.loads(result.pop(field) or "{}")
    return result


def session(conn, *, round_id=None, task_id=None):
    # Do not change a caller's row_factory.
    if not db_read_sqlite_master.table_exists(conn, "review_sessions"):
        return None
    row = round_records.session(conn, "review_sessions", round_id=round_id, task_id=task_id)
    return decode(dict(row)) if row is not None else None


def enqueue(conn, repo, event, route, workers, *, recovery_key=None):
    trigger = event["event_fingerprint"] + (":recovery:" + recovery_key if recovery_key else "")
    key = f"{repo['full_name']}:{event['number']}:{trigger}"
    round_id = "review-" + hashlib.sha256(key.encode()).hexdigest()[:24]
    request = {
        "repo": dict(repo),
        "event": dict(event),
        "route": dict(route),
        "workers": [dict(worker) for worker in workers],
    }
    timestamp = now()
    db_write_review_sessions.insert_for_enqueue(
        conn,
        round_id,
        repo["full_name"],
        event["number"],
        event["workstream_id"],
        trigger,
        json.dumps(request, ensure_ascii=False),
        timestamp,
        timestamp,
    )
    return session(conn, round_id=round_id)


def update(conn, round_id, **fields):
    allowed = {
        "task_id",
        "status",
        "manifest_json",
        "report_path",
        "report_sha256",
        "error",
    }
    if not fields or set(fields) - allowed:
        raise ValueError("invalid review session update")
    fields["updated_at"] = now()
    round_records.update_session(conn, "review_sessions", round_id, fields)


def runs(conn, round_id):
    cursor = db_read_review_runs.find_record_by_round_id(conn, round_id)
    return [decode(dict(row)) for row in cursor]


def new_run(conn, round_id, stage, worker_id, owner_attempt_id, root, prompt_sha256, model=None):
    attempt_no = db_records.first(db_read_review_runs.find_for_new_run(conn, round_id, stage, worker_id))[0]
    output_dir = Path(root) / ("runs" if stage == "review" else stage)
    if stage == "review":
        # Worker names are arbitrary config keys, never path components.
        output_dir /= hashlib.sha256(worker_id.encode()).hexdigest()[:16]
    # A rolled-back INSERT can leave its directory behind. Never reuse those
    # unregistered outputs, even when the next database attempt number is equal.
    run_id, timestamp = "review-run-" + uuid4().hex, now()
    output_dir /= f"attempt-{attempt_no}-{run_id}"
    output_dir.mkdir(parents=True, exist_ok=False)
    db_write_review_runs.insert_for_new_run(
        conn,
        run_id,
        round_id,
        stage,
        worker_id,
        model,
        attempt_no,
        owner_attempt_id,
        str(output_dir.resolve()),
        prompt_sha256,
        timestamp,
        timestamp,
    )
    return next(run for run in runs(conn, round_id) if run["run_id"] == run_id)


def update_run(conn, run_id, **fields):
    allowed = {"status", "dispatch_json", "artifact_json", "error", "slot_reserved"}
    if not fields or set(fields) - allowed:
        raise ValueError("invalid review run update")
    fields["updated_at"] = now()
    round_records.update_run(conn, "review_runs", run_id, fields)


def configure_process_pool(conn, limit):
    if type(limit) is not int or limit < 1:
        raise ValueError("model_max_concurrency must be a positive integer")
    db_write_review_process_pool.update_for_configure_process_pool(conn, limit)


def process_pool_status(conn):
    from robert_agent.persistence.rounds import pool as model_pool

    return model_pool.status(conn)


def reserved_runs(conn):
    cursor = db_read_review_runs.find_for_reserved_runs(conn)
    return [decode(dict(row)) for row in cursor]


def reserve_slot(conn, run_id):
    from robert_agent.persistence.rounds import pool as model_pool

    return model_pool.reserve(conn, "review_runs", run_id, now())


def release_slot(conn, run_id):
    db_write_review_runs.update_for_release_slot(conn, run_id)


def can_abandon(run):
    return run["status"] == ModelRunStatus.STARTING and not run["dispatch"] and bool(run.get("slot_reserved"))


def task_status(conn, task_id):
    entry = session(conn, task_id=task_id)
    if not entry:
        return None
    return {key: value for key, value in entry.items() if key != "request"} | {
        "process_pool": process_pool_status(conn),
        "runs": [
            {key: value for key, value in run.items() if key != "dispatch"} for run in runs(conn, entry["round_id"])
        ],
    }


def board_status(conn, task_id):
    """Only bounded public status; no filesystem paths, environment or raw logs."""
    from robert_agent.security.text import safe_text

    entry = session(conn, task_id=task_id)
    if not entry:
        return None
    manifest = entry["manifest"]
    return {
        "round_id": entry["round_id"],
        "status": entry["status"],
        "process_pool": process_pool_status(conn),
        "head_sha": manifest.get("head_sha"),
        "base_ref": manifest.get("base_ref"),
        "error": safe_text(entry["error"] or ""),
        "runs": [
            {
                "run_id": run["run_id"],
                "can_abandon": can_abandon(run),
                "stage": run["stage"],
                "worker_id": safe_text(run["worker_id"]),
                "model": safe_text(run["model"] or ""),
                "slot_reserved": bool(run["slot_reserved"]) if "slot_reserved" in run else None,
                "attempt_no": run["attempt_no"],
                "status": run["status"],
                "error": safe_text(run["error"] or ""),
            }
            for run in runs(conn, entry["round_id"])
        ],
    }


def release_finished_slot(conn, run):
    db_write_review_runs.update_for_release_finished_slot(conn, run["run_id"], run["updated_at"])
