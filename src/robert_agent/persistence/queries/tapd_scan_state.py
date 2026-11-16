"""Shared queries operations for tapd_scan_state."""

from sqlalchemy import LABEL_STYLE_NONE, bindparam, select

from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_state(conn, project_key):
    table = tables["tapd_scan_state"]
    query = (
        select(table.c["watermark_created"], table.c["watermark_comment_id"], table.c["pending_json"])
        .select_from(table)
        .where(table.c["project_key"] == bindparam("p0"))
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query, {"p0": project_key})
