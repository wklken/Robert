"""Bounded retention for diagnostic history, never task or publication state."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import time


COUNT_TABLES = ('run_steps', 'run_repo_steps', 'agent_runs', 'daemon_events', 'leases')


def _positive_int(value, name, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f'{name} must be a positive integer')
    if maximum is not None and value > maximum:
        raise ValueError(f'{name} must be at most {maximum}')
    return value


def _select_batch(conn, success_cutoff, failure_cutoff, event_cutoff, batch_size):
    runs = [row[0] for row in conn.execute(
        """SELECT r.run_id FROM agent_runs r
        WHERE ((r.status IN ('completed','skipped') AND julianday(r.finished_at) < julianday(?))
            OR (r.status = 'failed' AND julianday(r.finished_at) < julianday(?)))
          AND NOT EXISTS (SELECT 1 FROM wakeups w WHERE w.source_run_id = r.run_id)
          AND NOT EXISTS (SELECT 1 FROM wakeups w WHERE w.consumed_run_id = r.run_id)
          AND NOT EXISTS (SELECT 1 FROM leases l WHERE l.owner_id = r.run_id AND l.status = 'active')
          AND r.run_id != (SELECT run_id FROM agent_runs ORDER BY started_at DESC,run_id DESC LIMIT 1)
        ORDER BY julianday(r.finished_at),r.run_id LIMIT ?""",
        (success_cutoff, failure_cutoff, batch_size),
    )]
    events = [row[0] for row in conn.execute(
        """SELECT daemon_event_id FROM daemon_events
        WHERE (status IN ('ok','running','skipped') AND julianday(created_at) < julianday(?))
           OR (status NOT IN ('ok','running','skipped') AND julianday(created_at) < julianday(?))
        ORDER BY julianday(created_at),daemon_event_id LIMIT ?""",
        (event_cutoff, min(event_cutoff, failure_cutoff), batch_size),
    )]
    leases = [row[0] for row in conn.execute(
        """SELECT l.lease_id FROM leases l
        WHERE l.resource_type IN ('agent_run','daemon')
          AND ((l.status = 'released' AND julianday(l.released_at) < julianday(?))
            OR (l.status = 'expired' AND julianday(l.expires_at) < julianday(?)))
          AND NOT EXISTS (SELECT 1 FROM agent_runs r WHERE r.run_id = l.owner_id AND r.status = 'running')
          AND NOT EXISTS (SELECT 1 FROM daemon_runs d WHERE d.daemon_run_id = l.owner_id AND d.status = 'running')
        ORDER BY l.acquired_at,l.lease_id LIMIT ?""",
        (success_cutoff, failure_cutoff, batch_size),
    )]
    return runs, events, leases


def _prune_batch(conn, cutoffs, batch_size, apply):
    # Acquire the write lock before selecting candidates, so a new recovery
    # reference cannot appear between the selection and deletion.
    conn.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
    try:
        runs, events, leases = _select_batch(conn, *cutoffs, batch_size)
        groups = (
            ('run_steps', 'run_id', runs),
            ('run_repo_steps', 'run_id', runs),
            ('agent_runs', 'run_id', runs),
            ('daemon_events', 'daemon_event_id', events),
            ('leases', 'lease_id', leases),
        )
        counts = dict.fromkeys(COUNT_TABLES, 0)
        for table, key, identifiers in groups:
            if not identifiers:
                continue
            placeholders = ','.join('?' for _ in identifiers)
            where = f'{key} IN ({placeholders})'
            counts[table] = conn.execute(f'SELECT count(*) FROM {table} WHERE {where}', identifiers).fetchone()[0]
            if apply:
                conn.execute(f'DELETE FROM {table} WHERE {where}', identifiers)
        conn.commit()
        return counts
    except Exception:
        conn.rollback()
        raise


def prune_database(db_path, *, apply=False, now=None, batch_size=100, max_batches=1,
                   run_retention_days=7, failed_run_retention_days=30, event_retention_days=7,
                   max_seconds=None):
    """Inspect one batch by default; only apply=True deletes diagnostic rows.

    Each transaction selects at most batch_size runs, events and leases. Steps
    belonging to a selected run are removed atomically with that run. No VACUUM
    or schema migration is performed here. A missing database is never created.
    """
    _positive_int(batch_size, 'batch_size', maximum=500)
    _positive_int(max_batches, 'max_batches', maximum=1000)
    if max_seconds is not None and (isinstance(max_seconds, bool) or max_seconds <= 0):
        raise ValueError('max_seconds must be positive')
    started = time.monotonic()
    for name, value in (('run_retention_days', run_retention_days),
                        ('failed_run_retention_days', failed_run_retention_days),
                        ('event_retention_days', event_retention_days)):
        _positive_int(value, name)
    when = datetime.fromisoformat(now.replace('Z', '+00:00')) if now else datetime.now(timezone.utc)
    if when.tzinfo is None:
        raise ValueError('now must include a timezone')
    when = when.astimezone(timezone.utc)
    cutoffs = tuple((when - timedelta(days=days)).isoformat() for days in
                    (run_retention_days, failed_run_retention_days, event_retention_days))
    result = {'ok': True, 'status': 'pruned' if apply else 'planned', 'dry_run': not apply,
              'batch_size': batch_size, 'batches': 0, 'counts': dict.fromkeys(COUNT_TABLES, 0),
              'cutoffs': dict(zip(('successful_runs', 'failed_runs', 'daemon_events'), cutoffs)),
              'more_work': False}
    path = Path(db_path).expanduser().resolve()
    try:
        with closing(sqlite3.connect(path.as_uri() + ('?mode=rw' if apply else '?mode=ro'),
                                     uri=True, timeout=0.1)) as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            if not apply:
                conn.execute('PRAGMA query_only=ON')
            for _ in range(max_batches if apply else 1):
                if result['batches'] and max_seconds is not None and time.monotonic() - started >= max_seconds:
                    break
                counts = _prune_batch(conn, cutoffs, batch_size, apply)
                result['more_work'] = any(counts[t] >= batch_size for t in ('agent_runs', 'daemon_events', 'leases'))
                result['batches'] += 1
                for table, count in counts.items():
                    result['counts'][table] += count
                if not any(counts.values()):
                    break
    except sqlite3.Error as exc:
        # Python 3.10 does not expose sqlite_errorcode. Only lock contention is
        # retryable; malformed schemas and other I/O failures remain visible.
        busy = isinstance(exc, sqlite3.OperationalError) and str(exc) in {
            'database is locked', 'database table is locked', 'database schema is locked',
        }
        result.update(ok=False, status='busy' if busy else 'failed', safe_error=str(exc))
    return result
