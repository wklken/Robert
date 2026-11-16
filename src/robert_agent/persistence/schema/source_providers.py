"""Rebuild source tables whose constraints predate provider identities."""

from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3

from robert_agent.foundation.contracts.execution import RUNNING_ATTEMPT_STATUSES
from robert_agent.foundation.timestamps import utc_timestamp

_MAX_REPORTED_BLOCKERS = 5


class RebuildRefused(RuntimeError):
    """The legacy tables are unchanged; the message names what blocked the rebuild."""


def backup_path(db_path):
    db_path = Path(db_path)
    return db_path.with_name(db_path.name + ".pre-source-providers")


def _table_sql(conn, table):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return row[0] if row else None


def _columns(conn, table):
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _legacy_tables(conn):
    tables = []
    if _table_sql(conn, "github_sources") and "provider" not in _columns(conn, "github_sources"):
        tables.append("github_sources")
    work_items = _table_sql(conn, "work_items")
    if work_items and "'tapd'" not in work_items:
        tables.append("work_items")
    return tables


def _blockers(conn, now):
    blockers = []
    if _table_sql(conn, "leases"):
        for lease_id, resource_type, expires_at in conn.execute(
            "SELECT lease_id, resource_type, expires_at FROM leases WHERE status = 'active' ORDER BY lease_id"
        ):
            expiry = utc_timestamp(expires_at)
            if expiry is None or expiry > now:
                blockers.append(f"{resource_type} lease {lease_id} is active until {expires_at}")
    if _table_sql(conn, "attempts"):
        statuses = sorted(RUNNING_ATTEMPT_STATUSES)
        rows = conn.execute(
            f"SELECT attempt_id, status FROM attempts WHERE status IN ({', '.join('?' for _ in statuses)}) "
            "ORDER BY attempt_id",
            statuses,
        )
        blockers.extend(f"attempt {attempt_id} is {status}" for attempt_id, status in rows)
    if _table_sql(conn, "execution_subjects"):
        rows = conn.execute(
            "SELECT json_extract(metadata_json, '$.dispatch_inflight'), subject_id FROM execution_subjects "
            "WHERE json_extract(metadata_json, '$.dispatch_inflight') IS NOT NULL ORDER BY subject_id"
        )
        blockers.extend(
            f"Worker launch {attempt_id} for {subject_id} is unconfirmed" for attempt_id, subject_id in rows
        )
    for table, label in (("review_runs", "review run"), ("vote_runs", "vote run")):
        if _table_sql(conn, table) and "slot_reserved" in _columns(conn, table):
            rows = conn.execute(f"SELECT run_id FROM {table} WHERE slot_reserved = 1 ORDER BY run_id")
            blockers.extend(f"{label} {run_id} has not been proven to exit" for (run_id,) in rows)
    return blockers


def _create_statement(schema, table):
    start = schema.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    end = schema.index(";", start) + 1
    while not sqlite3.complete_statement(schema[start:end]):
        end = schema.index(";", end) + 1
    return schema[start:end]


def _backup(conn, db_path):
    target = backup_path(db_path)
    staging = target.with_name(target.name + ".tmp")
    staging.unlink(missing_ok=True)
    with closing(sqlite3.connect(staging)) as copy:
        conn.backup(copy)
    os.replace(staging, target)


def _rebuild(conn, table, create_sql):
    staged = f"{table}_rebuild"
    legacy_columns = _columns(conn, table)
    # DROP TABLE removes the table's indexes and triggers; restore them from their stored definitions.
    dependents = [
        sql
        for (sql,) in conn.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL "
            "ORDER BY type, name",
            (table,),
        )
    ]
    conn.execute(create_sql.replace(f"CREATE TABLE IF NOT EXISTS {table} (", f"CREATE TABLE {staged} (", 1))
    unknown = sorted(set(legacy_columns) - set(_columns(conn, staged)))
    if unknown:
        raise RebuildRefused(f"{table} has columns the current schema does not define: {', '.join(unknown)}")
    names = ", ".join(f'"{column}"' for column in legacy_columns)
    conn.execute(f"INSERT INTO {staged} ({names}) SELECT {names} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {staged} RENAME TO {table}")
    for sql in dependents:
        conn.execute(sql)


def _row_counts(conn, tables):
    return {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}


def _foreign_key_violations(conn):
    violations = Counter((child, parent) for child, _rowid, parent, _key in conn.execute("PRAGMA foreign_key_check"))
    return ", ".join(f"{child} -> {parent}: {count} rows" for (child, parent), count in sorted(violations.items()))


def rebuild_legacy_tables(db_path, schema, *, now=None):
    """Rebuild legacy source tables in one transaction while no process can write results.

    Foreign keys must stay off: dropping a referenced table with them on cascades into its children.
    The checks and the backup share one read snapshot, and SQLite refuses to back up a database from a
    connection that already holds the writer lock. If another process writes after that snapshot,
    the first rebuild write fails and nothing changes.
    """
    now = now or datetime.now(timezone.utc)
    with closing(sqlite3.connect(db_path, isolation_level=None)) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        if not _legacy_tables(conn):
            return []
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        try:
            tables = _legacy_tables(conn)
            blockers = _blockers(conn, now) if tables else []
            if blockers:
                shown = "; ".join(blockers[:_MAX_REPORTED_BLOCKERS])
                more = len(blockers) - _MAX_REPORTED_BLOCKERS
                raise RebuildRefused(
                    "source table rebuild refused until Robert is stopped and running work has finished: "
                    + shown
                    + (f"; and {more} more" if more > 0 else "")
                    + ". Use the previous Robert version to finish or reconcile work and confirm exit, "
                    "then stop it normally before retrying the upgrade."
                )
            if tables:
                _backup(conn, db_path)
                counts = _row_counts(conn, tables)
                for table in tables:
                    _rebuild(conn, table, _create_statement(schema, table))
                if _row_counts(conn, tables) != counts:
                    raise RebuildRefused("source table rebuild changed row counts")
                violations = _foreign_key_violations(conn)
                if violations:
                    raise RebuildRefused(f"source table rebuild failed the foreign key check: {violations}")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    return tables


def initialize(conn):
    """Add the workstream public reference inside the caller's initialization transaction."""
    if "public_ref" not in _columns(conn, "workstreams"):
        conn.execute("ALTER TABLE workstreams ADD COLUMN public_ref TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_workstreams_public_ref ON workstreams(public_ref) "
        "WHERE public_ref IS NOT NULL"
    )
