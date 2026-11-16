"""Shared stores operations for tapd_scan_state."""

from sqlalchemy import bindparam
from sqlalchemy.dialects.sqlite import insert

from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def save_watermark(conn, project_key, created, comment_id, now):
    stmt = insert(tables["tapd_scan_state"]).values(
        {
            "project_key": bindparam("p0"),
            "watermark_created": bindparam("p1"),
            "watermark_comment_id": bindparam("p2"),
            "updated_at": bindparam("p3"),
        }
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["project_key"],
        set_={
            "watermark_created": stmt.excluded["watermark_created"],
            "watermark_comment_id": stmt.excluded["watermark_comment_id"],
            "updated_at": stmt.excluded["updated_at"],
        },
    )
    return statements.write(conn, stmt, {"p0": project_key, "p1": created, "p2": comment_id, "p3": now})
