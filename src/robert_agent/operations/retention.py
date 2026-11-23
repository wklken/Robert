"""Bounded retention for diagnostic history, never task or publication state."""

from robert_agent.persistence.stores import diagnostics as diagnostic_store

from robert_agent.persistence import transactions as db_transactions

from robert_agent.persistence import database as db_database
from robert_agent.persistence import errors as db_errors
from robert_agent.persistence.dialects import sqlite as db_sqlite

from robert_agent.persistence.queries import agent_runs as db_read_agent_runs
from robert_agent.persistence.queries import diagnostics as diagnostic_queries
from robert_agent.persistence.queries import daemon_events as db_read_daemon_events
from robert_agent.persistence.queries import leases as db_read_leases
from robert_agent.persistence.queries.run_summaries import shared_schema_available

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time


COUNT_TABLES = ("run_steps", "run_repo_steps", "agent_runs", "daemon_events", "leases")


def _positive_int(value, name, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def _select_batch(conn, success_cutoff, failure_cutoff, event_cutoff, batch_size):
    runs = [
        row[0] for row in db_read_agent_runs.find_for_select_batch(conn, success_cutoff, failure_cutoff, batch_size)
    ]
    events = [
        row[0]
        for row in db_read_daemon_events.find_for_select_batch(
            conn, event_cutoff, min(event_cutoff, failure_cutoff), batch_size
        )
    ]
    leases = [row[0] for row in db_read_leases.find_for_select_batch(conn, success_cutoff, failure_cutoff, batch_size)]
    return runs, events, leases


def _prune_batch(conn, cutoffs, batch_size, apply):
    # Acquire the write lock before selecting candidates, so a new recovery
    # reference cannot appear between the selection and deletion.
    db_transactions.begin(conn, immediate=apply)
    try:
        runs, events, leases = _select_batch(conn, *cutoffs, batch_size)
        compactable = diagnostic_queries.compactable_runs(conn, batch_size, excluded_run_ids=runs)
        excluded = [*runs, *compactable]
        shared = shared_schema_available(conn)
        summaries = diagnostic_queries.summary_deduplication_candidates(conn, batch_size, excluded) if shared else []
        historical_steps = diagnostic_queries.matching_step_runs(conn, batch_size, excluded)
        unused = diagnostic_queries.unused_shared_summaries(conn, batch_size, runs) if shared else []
        counts = diagnostic_store.prune_rows(conn, runs, events, leases, apply=apply)
        compacted = diagnostic_store.compact_run_details(conn, compactable, apply=apply, excluded_lease_ids=leases)
        for table, count in compacted.items():
            counts[table] += count
        shared_ids = diagnostic_store.deduplicate_summaries(conn, summaries, apply=apply)
        unused = [identifier for identifier in unused if identifier not in shared_ids]
        step_count = diagnostic_store.reference_historical_steps(conn, historical_steps, apply=apply)
        diagnostic_store.prune_shared_summaries(conn, unused, apply=apply)
        conn.commit()
        return counts, len(summaries), step_count, len(unused)
    except Exception:
        conn.rollback()
        raise


def prune_database(
    db_path,
    *,
    apply=False,
    now=None,
    batch_size=100,
    max_batches=1,
    run_retention_days=7,
    failed_run_retention_days=30,
    event_retention_days=7,
    max_seconds=None,
):
    """Inspect one batch by default; only apply=True deletes diagnostic rows.

    Each transaction selects at most batch_size runs, events and leases. Steps
    belonging to a selected run are removed atomically with that run. No VACUUM
    or schema migration is performed here. A missing database is never created.
    Successful no-action details outside the newest 200 runs are compacted,
    retaining summaries, referenced runs and latest repository route progress.
    Identical compacted summaries share storage; matching historical step
    outputs reference their run summary without losing decoded content.
    """
    _positive_int(batch_size, "batch_size", maximum=500)
    _positive_int(max_batches, "max_batches", maximum=1000)
    if max_seconds is not None and (isinstance(max_seconds, bool) or max_seconds <= 0):
        raise ValueError("max_seconds must be positive")
    started = time.monotonic()
    for name, value in (
        ("run_retention_days", run_retention_days),
        ("failed_run_retention_days", failed_run_retention_days),
        ("event_retention_days", event_retention_days),
    ):
        _positive_int(value, name)
    when = datetime.fromisoformat(now.replace("Z", "+00:00")) if now else datetime.now(timezone.utc)
    if when.tzinfo is None:
        raise ValueError("now must include a timezone")
    when = when.astimezone(timezone.utc)
    cutoffs = tuple(
        (when - timedelta(days=days)).isoformat()
        for days in (run_retention_days, failed_run_retention_days, event_retention_days)
    )
    result = {
        "ok": True,
        "status": "pruned" if apply else "planned",
        "dry_run": not apply,
        "batch_size": batch_size,
        "batches": 0,
        "counts": dict.fromkeys(COUNT_TABLES, 0),
        "cutoffs": dict(zip(("successful_runs", "failed_runs", "daemon_events"), cutoffs)),
        "more_work": False,
        "deduplicated_summaries": 0,
        "referenced_step_outputs": 0,
        "pruned_shared_summaries": 0,
    }
    path = Path(db_path).expanduser().resolve()
    try:
        with closing(
            db_database.connect(path.as_uri() + ("?mode=rw" if apply else "?mode=ro"), uri=True, timeout=0.1)
        ) as conn:
            db_sqlite.configure(conn, foreign_keys=True)
            if not apply:
                db_sqlite.configure(conn, query_only=True)
            for _ in range(max_batches if apply else 1):
                if result["batches"] and max_seconds is not None and time.monotonic() - started >= max_seconds:
                    break
                counts, summaries, steps, unused = _prune_batch(conn, cutoffs, batch_size, apply)
                result["more_work"] = any(counts[t] >= batch_size for t in COUNT_TABLES) or any(
                    count >= batch_size for count in (summaries, steps, unused)
                )
                result["deduplicated_summaries"] += summaries
                result["referenced_step_outputs"] += steps
                result["pruned_shared_summaries"] += unused
                result["batches"] += 1
                for table, count in counts.items():
                    result["counts"][table] += count
                if not any(counts.values()) and not (summaries or steps or unused):
                    break
    except db_errors.Error as exc:
        # Python 3.10 does not expose sqlite_errorcode. Only lock contention is
        # retryable; malformed schemas and other I/O failures remain visible.
        busy = isinstance(exc, db_errors.OperationalError) and str(exc) in {
            "database is locked",
            "database table is locked",
            "database schema is locked",
        }
        result.update(ok=False, status="busy" if busy else "failed", safe_error=str(exc))
    return result
