"""Preserve existing task-owned action and receipt IDs when adding native owners."""

from robert_agent.persistence.schema import native_sources

_LEDGER_COLUMN = (
    "source_result_id TEXT REFERENCES source_results(source_result_id) CHECK (source_result_id IS NULL OR "
    "(request_id IS NOT NULL AND state = 'consumed' AND consumed_at IS NOT NULL AND result_id IS NULL))"
)


def requires_action_owner_upgrade(conn):
    """Detect both the task-only and the earlier TAPD-only action owner constraints."""
    if any(row[1] == "task_id" and row[3] for row in conn.execute("PRAGMA table_info(github_actions)")):
        return True
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'github_actions'").fetchone()
    return row is not None and "result_id IS NULL AND provider = 'tapd' AND action_type = 'comment'" in row[0]


def upgrade(conn, schema):
    if requires_action_owner_upgrade(conn):
        if conn.execute("PRAGMA foreign_keys").fetchone()[0]:
            raise RuntimeError("Source result migration requires a dedicated rebuild connection")
        native_sources.check_quiescent(conn)
        native_sources.replace_table_preserving_rows(conn, "github_actions", schema)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(request_events)")}
    if columns and "source_result_id" not in columns:
        conn.execute("ALTER TABLE request_events ADD COLUMN " + _LEDGER_COLUMN)
