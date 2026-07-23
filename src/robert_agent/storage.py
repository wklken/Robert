"""SQLite storage helpers for the rewritten DD GitHub agent."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from robert_agent.resource_files import resource


SCHEMA_RESOURCE = resource("db", "schema.sql")
WORK_ITEM_MIGRATION_VERSION = 2
WORK_ITEM_MIGRATION_NAME = "robert-work-item-control-v1"
WORK_ITEM_MIGRATION_CHECKSUM = hashlib.sha256(
    WORK_ITEM_MIGRATION_NAME.encode("utf-8")
).hexdigest()


def _ensure_column(conn, table, column_name, ddl):
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _origin_workstream_id(payload):
    metadata = payload.get("metadata") or {}
    dd_workstream = metadata.get("dd_workstream") or {}
    return (
        payload.get("origin_workstream_id")
        or dd_workstream.get("origin_workstream_id")
        or dd_workstream.get("workstream_id")
    )


def _has_active_task(conn, workstream_id):
    row = conn.execute(
        """
        SELECT 1
        FROM tasks
        WHERE workstream_id = ?
          AND lifecycle IN ('detected', 'authorized', 'classified', 'queued', 'running')
        LIMIT 1
        """,
        (workstream_id,),
    ).fetchone()
    return bool(row)


def _repair_origin_workstream_after_task_move(conn, origin_workstream_id, task_id, now):
    row = conn.execute(
        "SELECT active_task_id FROM workstreams WHERE workstream_id = ?",
        (origin_workstream_id,),
    ).fetchone()
    if not row or row[0] != task_id:
        return
    lifecycle = "active" if _has_active_task(conn, origin_workstream_id) else "completed"
    conn.execute(
        """
        UPDATE workstreams
        SET lifecycle = ?, active_task_id = NULL, updated_at = ?
        WHERE workstream_id = ?
        """,
        (lifecycle, now, origin_workstream_id),
    )


def _migrate_pr_mainline_workstreams(conn):
    _ensure_column(
        conn,
        "workstreams",
        "origin_workstream_id",
        "origin_workstream_id TEXT REFERENCES workstreams(workstream_id) ON DELETE SET NULL",
    )
    rows = conn.execute(
        """
        SELECT DISTINCT t.task_id, t.lifecycle, gs.repo_id, gs.source_id, gs.source_key, ge.payload_json
        FROM tasks t
        JOIN task_events te ON te.task_id = t.task_id
        JOIN github_events ge ON ge.event_id = te.event_id
        JOIN github_sources gs ON gs.source_id = ge.source_id
        WHERE gs.source_type = 'pull_request'
          AND t.workstream_id != gs.source_key
        """
    ).fetchall()
    now = conn.execute("SELECT datetime('now')").fetchone()[0]
    for task_id, task_lifecycle, repo_id, source_id, source_key, payload_json in rows:
        try:
            payload = json.loads(payload_json or "{}")
        except ValueError:
            payload = {}
        origin_workstream_id = _origin_workstream_id(payload)
        if not origin_workstream_id:
            continue
        active_task_id = task_id if task_lifecycle in {
            "detected",
            "authorized",
            "classified",
            "queued",
            "running",
        } else None
        workstream_lifecycle = "active" if active_task_id else "completed"
        conn.execute(
            """
            INSERT INTO workstreams(
              workstream_id, repo_id, primary_source_id, origin_workstream_id,
              lifecycle, active_task_id, created_at, updated_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), '{}')
            ON CONFLICT(workstream_id) DO UPDATE SET
              origin_workstream_id = excluded.origin_workstream_id,
              primary_source_id = excluded.primary_source_id,
              lifecycle = excluded.lifecycle,
              active_task_id = excluded.active_task_id,
              updated_at = excluded.updated_at
            """,
            (
                source_key,
                repo_id,
                source_id,
                origin_workstream_id,
                workstream_lifecycle,
                active_task_id,
            ),
        )
        conn.execute(
            "UPDATE tasks SET workstream_id = ? WHERE task_id = ?",
            (source_key, task_id),
        )
        conn.execute(
            """
            INSERT INTO workstream_sources(workstream_id, source_id, relationship, created_at)
            VALUES (?, ?, 'derived_pr', datetime('now'))
            ON CONFLICT(workstream_id, source_id) DO NOTHING
            """,
            (source_key, source_id),
        )
        _repair_origin_workstream_after_task_move(conn, origin_workstream_id, task_id, now)


def _migrate_github_action_publish_status(conn):
    _ensure_column(
        conn,
        "github_actions",
        "publish_status",
        "publish_status TEXT NOT NULL DEFAULT 'not_published' CHECK (publish_status IN ('not_published', 'published', 'skipped'))",
    )


def _migrate_route_recommended_skills(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(route_decisions)")}
    _ensure_column(
        conn,
        "route_decisions",
        "recommended_skills_json",
        "recommended_skills_json TEXT NOT NULL DEFAULT '[]'",
    )
    source_column = "allowed_skills_json" if "allowed_skills_json" in columns else "required_skills_json"
    conn.execute(
        f"""
        UPDATE route_decisions
        SET recommended_skills_json = {source_column}
        WHERE recommended_skills_json = '[]'
          AND {source_column} != '[]'
        """
    )


def _migrate_project_memory_fts(conn):
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS project_memory_fts USING fts5(
              memory_id UNINDEXED,
              repo_id UNINDEXED,
              title,
              short_summary,
              long_summary,
              paths,
              symbols,
              keywords
            )
            """
        )
    except sqlite3.OperationalError:
        return


def _backfill_work_item_id(workstream_id):
    digest = hashlib.sha256(workstream_id.encode("utf-8")).hexdigest()[:20]
    return f"wi-{digest}"


def _backfill_event_id(workstream_id):
    digest = hashlib.sha256(f"backfill:{workstream_id}".encode("utf-8")).hexdigest()[:20]
    return f"wie-{digest}"


def _backfill_work_items(conn):
    rows = conn.execute(
        """
        SELECT
          w.workstream_id,
          w.repo_id,
          w.lifecycle,
          w.created_at,
          w.updated_at,
          w.primary_source_id,
          gs.title,
          gs.author_login,
          COALESCE(
            (
              SELECT t.priority
              FROM tasks t
              WHERE t.workstream_id = w.workstream_id
              ORDER BY t.created_at DESC, t.task_id DESC
              LIMIT 1
            ),
            'P2'
          ) AS priority
        FROM workstreams w
        LEFT JOIN github_sources gs ON gs.source_id = w.primary_source_id
        WHERE w.origin_workstream_id IS NULL
        ORDER BY w.created_at, w.workstream_id
        """
    ).fetchall()
    for (
        workstream_id,
        repo_id,
        lifecycle,
        created_at,
        updated_at,
        primary_source_id,
        source_title,
        author_login,
        priority,
    ) in rows:
        work_item_id = _backfill_work_item_id(workstream_id)
        created_by = author_login or "system"
        completed_at = updated_at if lifecycle == "completed" else None
        canceled_at = updated_at if lifecycle == "canceled" else None
        conn.execute(
            """
            INSERT INTO work_items(
              work_item_id, repo_id, title, description, priority,
              origin_type, origin_source_id, routing_mode, requested_worker,
              workstream_id, creation_idempotency_key, created_by,
              activated_at, completed_at, canceled_at, version,
              created_at, updated_at, metadata_json
            )
            VALUES (?, ?, ?, '', ?, 'github', ?, 'auto', NULL, ?, ?, ?, ?, ?, ?, 1, ?, ?, '{}')
            ON CONFLICT DO NOTHING
            """,
            (
                work_item_id,
                repo_id,
                source_title or f"Workstream {workstream_id}",
                priority if priority in {"P0", "P1", "P2", "P3"} else "P2",
                primary_source_id,
                workstream_id,
                f"backfill:{workstream_id}",
                created_by,
                created_at,
                completed_at,
                canceled_at,
                created_at,
                updated_at,
            ),
        )
        item = conn.execute(
            "SELECT work_item_id FROM work_items WHERE workstream_id = ?",
            (workstream_id,),
        ).fetchone()
        if not item:
            continue
        conn.execute(
            """
            INSERT INTO work_item_events(
              event_id, work_item_id, event_type, actor_kind, actor_identity,
              body, resolves_event_id, idempotency_key, created_at, metadata_json
            )
            VALUES (?, ?, 'backfilled', 'system', 'migration', '', NULL, ?, ?, ?)
            ON CONFLICT(work_item_id, idempotency_key) DO NOTHING
            """,
            (
                _backfill_event_id(workstream_id),
                item[0],
                f"backfill:{workstream_id}",
                updated_at,
                json.dumps(
                    {"source": "workstream", "workstream_id": workstream_id},
                    sort_keys=True,
                ),
            ),
        )


def _migrate_work_item_control(conn):
    marker = conn.execute(
        "SELECT checksum FROM schema_migrations WHERE name = ?",
        (WORK_ITEM_MIGRATION_NAME,),
    ).fetchone()
    if marker:
        if marker[0] != WORK_ITEM_MIGRATION_CHECKSUM:
            raise RuntimeError("work item migration checksum mismatch")
        return

    _ensure_column(
        conn,
        "tasks",
        "routing_mode",
        "routing_mode TEXT NOT NULL DEFAULT 'auto' CHECK (routing_mode IN ('auto', 'manual'))",
    )
    _ensure_column(conn, "tasks", "requested_worker", "requested_worker TEXT")
    _ensure_column(
        conn,
        "wakeups",
        "work_item_id",
        "work_item_id TEXT REFERENCES work_items(work_item_id) ON DELETE CASCADE",
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wakeups_pending_work_item
        ON wakeups(work_item_id, status, not_before_at)
        WHERE work_item_id IS NOT NULL
        """
    )
    _backfill_work_items(conn)
    conn.execute(
        """
        INSERT INTO schema_migrations(version, name, checksum, applied_at)
        VALUES (?, ?, ?, datetime('now'))
        """,
        (
            WORK_ITEM_MIGRATION_VERSION,
            WORK_ITEM_MIGRATION_NAME,
            WORK_ITEM_MIGRATION_CHECKSUM,
        ),
    )


def schema_sql() -> str:
    return SCHEMA_RESOURCE.read_text(encoding="utf-8")


def init_database(db_path, schema_path=None):
    db_path = Path(db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if schema_path is None:
        schema = schema_sql()
        schema_path = SCHEMA_RESOURCE
    else:
        schema_path = Path(schema_path)
        schema = schema_path.read_text(encoding="utf-8")

    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.executescript(schema)
        _migrate_pr_mainline_workstreams(conn)
        _migrate_github_action_publish_status(conn)
        _migrate_route_recommended_skills(conn)
        _migrate_work_item_control(conn)
        _migrate_project_memory_fts(conn)
        tables = sorted(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        )

    return {
        "ok": True,
        "status": "initialized",
        "db_path": str(db_path),
        "schema_path": str(schema_path),
        "tables": tables,
    }
