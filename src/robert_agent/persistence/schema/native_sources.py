"""Transactional conversion of legacy GitHub source and event relations."""

import hashlib
import json
import sqlite3

from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence.rounds.source_evidence import source_model_blocker
from robert_agent.persistence.source_identity import github_source_host


VERSION = 7
NAME = "robert-native-sources-v1"
CHECKSUM = hashlib.sha256(NAME.encode("utf-8")).hexdigest()
_LEGACY_TABLES = {"github_sources": "sources", "github_events": "source_events"}


def table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def has_legacy_tables(conn):
    return any(table_exists(conn, name) for name in _LEGACY_TABLES)


def _refuse(kind, key):
    raise RuntimeError(
        f"Native source migration requires the old service to stop and reconcile {kind} {key!r} before cutover"
    )


def _object(raw, kind, key):
    try:
        result = json.loads(raw)
    except (TypeError, ValueError):
        _refuse(kind, key)
    if not isinstance(result, dict):
        _refuse(kind, key)
    return result


def _check_workers(conn):
    if table_exists(conn, "execution_subjects"):
        for subject_id, raw in conn.execute("SELECT subject_id, metadata_json FROM execution_subjects"):
            if _object(raw, "execution subject metadata", subject_id).get("dispatch_inflight"):
                _refuse("Worker launch reservation", subject_id)
    if not table_exists(conn, "attempts"):
        return
    for attempt_id, status, raw in conn.execute("SELECT attempt_id, status, metadata_json FROM attempts"):
        if status in {"running", "stale"}:
            _refuse("Worker attempt", attempt_id)
        metadata = _object(raw, "Worker attempt metadata", attempt_id)
        dispatch = metadata.get("dispatch")
        if dispatch is not None and not isinstance(dispatch, dict):
            _refuse("Worker dispatch evidence", attempt_id)
        dispatch = dispatch or {}
        launched = (
            dispatch.get("cleanup_pending") is True
            or dispatch.get("pid") is not None
            or dispatch.get("status") == "running"
            or metadata.get("worker_started_at")
        )
        # A Popen leader exit does not establish the process group exited.
        termination = metadata.get("termination")
        exit_proven = (
            utc_timestamp(metadata.get("process_stopped_at"))
            or (
                isinstance(termination, dict)
                and termination.get("status") in {"terminated", "not_found", "already_dead"}
            )
            or dispatch.get("cleanup_pending") is False
        )
        if launched and not exit_proven:
            _refuse("Worker exit", attempt_id)


def _table_columns(conn, name):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({name})")}


def _check_models(conn):
    for table in ("review_runs", "vote_runs"):
        if not table_exists(conn, table):
            continue
        columns = _table_columns(conn, table)
        reserved = "slot_reserved" if "slot_reserved" in columns else "NULL"
        dispatch = "dispatch_json" if "dispatch_json" in columns else "NULL"
        for run_id, status, slot_owned, raw in conn.execute(
            f"SELECT run_id, status, {reserved}, {dispatch} FROM {table}"
        ):
            if slot_owned or status in {"starting", "running"}:
                _refuse(f"{table} run", run_id)
            recorded = {} if raw is None else _object(raw, f"{table} dispatch evidence", run_id)
            if recorded.get("cleanup_pending") is True:
                _refuse(f"{table} cleanup", run_id)
            if recorded.get("cleanup_pending") is False:
                continue
            # With persisted slot ownership, release is recorded only after
            # group exit/cleanup. Legacy rows predating this field lack proof.
            if slot_owned == 0 and raw is not None:
                continue
            if (
                status not in {"prepared", "waiting", "interrupted", "abandoned"}
                or (slot_owned is None and status not in {"prepared", "waiting"})
                or recorded.get("pid") is not None
            ):
                _refuse(f"{table} exit", run_id)
    if table_exists(conn, "source_model_runs"):
        fields = ("run_id", "status", "slot_reserved", "launch_id", "launch_token", "dispatch_json", "exit_json")
        for values in conn.execute("SELECT " + ", ".join(fields) + " FROM source_model_runs"):
            run = dict(zip(fields, values))
            if source_model_blocker(run) not in (None, "model_waiting"):
                _refuse("source model run exit", run["run_id"])
    if table_exists(conn, "supervision_checkpoints"):
        for key, raw in conn.execute(
            """
            SELECT scope_type, metadata_json FROM supervision_checkpoints
            WHERE scope_id = 'installation'
              AND ((scope_type = 'decision_runner' AND rule_id = 'current')
                OR (scope_type = 'diagnostic' AND rule_id = 'runner'))
            """
        ):
            state = _object(raw, "supervision model runner", key)
            if state.get("claim") or state.get("attention_claims"):
                _refuse("supervision model launch", key)


def _check_publication(conn):
    if table_exists(conn, "github_control_children"):
        child = conn.execute(
            "SELECT child_id FROM github_control_children WHERE status != 'stopped' LIMIT 1"
        ).fetchone()
        if child:
            _refuse("scan or publication child", child[0])
    for table, key, states in (
        ("publication_intents", "publication_id", ("executing", "attention")),
        ("dispatch_requests", "request_id", ("creating",)),
    ):
        if not table_exists(conn, table):
            continue
        placeholders = ", ".join("?" for _ in states)
        row = conn.execute(f"SELECT {key} FROM {table} WHERE status IN ({placeholders}) LIMIT 1", states).fetchone()
        if row:
            _refuse("publication intent", row[0])


def _create_replacement(conn, table, schema):
    prefix = f"CREATE TABLE IF NOT EXISTS {table} ("
    start = schema.index(prefix) + len(prefix)
    end = schema.index("\n);", start)
    conn.execute(f"CREATE TABLE new_{table} (" + schema[start:end] + "\n)")


def _copy_sources(conn):
    rows = conn.execute(
        """
        SELECT s.source_id, s.repo_id, s.source_key, s.source_type, s.number, s.html_url,
               s.title, s.state, s.author_login, s.source_updated_at, s.metadata_json, r.full_name
        FROM sources s LEFT JOIN repos r ON r.repo_id = s.repo_id
        """
    )
    for *fields, project_key in rows:
        source_id, _, _, source_type, number, html_url = fields[:6]
        try:
            if not project_key:
                raise ValueError("stored repository identity is missing")
            host = github_source_host(project_key, source_type, number, html_url)
            conn.execute(
                """
                INSERT INTO new_sources(
                    source_id, repo_id, source_key, source_type, number, html_url,
                    title, state, author_login, source_updated_at, metadata_json,
                    provider, host, project_key, external_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'github', ?, ?, ?)
                """,
                (*fields, host, project_key, str(number)),
            )
        except (ValueError, sqlite3.IntegrityError) as exc:
            raise RuntimeError(
                f"Native source migration cannot establish a unique identity for {source_id!r}: {exc}"
            ) from exc


def _copy_events(conn):
    actor_kind = "actor_kind" if "actor_kind" in _table_columns(conn, "source_events") else "'github_user'"
    conn.execute(
        f"""
        INSERT INTO new_source_events(
            event_id, repo_id, source_id, event_fingerprint, event_type, actor_kind,
            actor_login, author_association, authorization_status, event_at, payload_json
        )
        SELECT event_id, repo_id, source_id, event_fingerprint, event_type, {actor_kind},
               actor_login, author_association, authorization_status, event_at, payload_json
        FROM source_events
        """
    )


def replace_table_preserving_rows(conn, table, schema, copier=None):
    # Unknown user-added columns are not silently discarded by the rebuild.
    _create_replacement(conn, table, schema)
    old_columns = _table_columns(conn, table)
    if old_columns - _table_columns(conn, f"new_{table}"):
        raise RuntimeError(f"Native source migration cannot preserve unexpected columns in {table}")
    indexes_and_triggers = conn.execute(
        "SELECT name, sql FROM sqlite_schema WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    if copier is None:
        columns = ", ".join(sorted(old_columns))
        conn.execute(f"INSERT INTO new_{table} ({columns}) SELECT {columns} FROM {table}")
    else:
        copier(conn)
    expected = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    actual = conn.execute(f"SELECT count(*) FROM new_{table}").fetchone()[0]
    if expected != actual:
        raise RuntimeError(f"Native source migration changed {table} row count from {expected} to {actual}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE new_{table} RENAME TO {table}")
    for name, sql in indexes_and_triggers:
        if name != "idx_github_events_recent":
            conn.execute(sql)


def check_quiescent(conn):
    _check_workers(conn)
    _check_models(conn)
    _check_publication(conn)


def migrate(conn, schema):
    """Rebuild physical tables with FK actions disabled only on this connection."""
    if conn.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("Native source migration requires a dedicated connection with foreign keys disabled")
    check_quiescent(conn)
    # The modern ALTER rewrites every reference, including child tables whose
    # rows do not move. Rebuild under the final names so references stay intact.
    for old, new in _LEGACY_TABLES.items():
        if table_exists(conn, old):
            if table_exists(conn, new):
                raise RuntimeError(f"Native source migration found both {old} and {new}")
            conn.execute(f"ALTER TABLE {old} RENAME TO {new}")
    for table, copier in (("sources", _copy_sources), ("source_events", _copy_events), ("work_items", None)):
        if table_exists(conn, table):
            replace_table_preserving_rows(conn, table, schema, copier)


def check_foreign_keys(conn):
    invalid = conn.execute("PRAGMA foreign_key_check").fetchone()
    if invalid:
        child, rowid, parent, _ = invalid
        raise RuntimeError(
            f"Native source migration foreign key check failed for {child} row {rowid} referencing {parent}"
        )
