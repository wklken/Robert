"""Narrow the old global source index while preserving WorkItem identity."""

from robert_agent.persistence.schema import native_sources

_NON_TAPD = "origin_source_id IS NOT NULL AND origin_type != 'tapd'"
_TAPD = "origin_source_id IS NOT NULL AND origin_type = 'tapd'"


def upgrade(conn):
    current = conn.execute(
        "SELECT sql FROM sqlite_schema WHERE type='index' AND name='idx_work_items_origin_source'"
    ).fetchone()
    if not current or _NON_TAPD.lower() in " ".join(current[0].lower().split()):
        return
    # The caller already holds the schema writer. Keep the old index and all
    # associations intact if another worker or effect still needs recovery.
    native_sources.check_quiescent(conn)
    conn.execute("DROP INDEX idx_work_items_origin_source")
    conn.execute("CREATE UNIQUE INDEX idx_work_items_origin_source ON work_items(origin_source_id) WHERE " + _NON_TAPD)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_work_items_tapd_origin_repo "
        "ON work_items(origin_source_id, repo_id) WHERE " + _TAPD
    )
