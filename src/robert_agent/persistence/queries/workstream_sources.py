"""Shared queries operations for workstream_sources."""

from sqlalchemy import select, bindparam, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_build_workstream(conn, workstream_id):
    table1 = tables["workstream_sources"].alias("ws")
    table2 = tables["github_sources"].alias("gs")
    query3 = (
        select(
            table1.c["relationship"],
            table2.c["source_key"],
            table2.c["source_type"],
            table2.c["number"],
            table2.c["html_url"],
            table2.c["state"],
            table2.c["title"],
        )
        .select_from(table1.join(table2, (table2.c["source_id"] == table1.c["source_id"]), isouter=False))
        .where((table1.c["workstream_id"] == bindparam("p0")))
        .order_by(table1.c["created_at"].desc())
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": workstream_id})


def find_for_get_work_item(conn, workstream_id):
    table1 = tables["workstream_sources"].alias("ws")
    table2 = tables["github_sources"].alias("gs")
    query3 = (
        select(
            table1.c["relationship"],
            table2.c["source_id"],
            table2.c["source_key"],
            table2.c["source_type"],
            table2.c["number"],
            table2.c["title"],
            table2.c["html_url"],
            table2.c["state"],
        )
        .select_from(table1.join(table2, (table2.c["source_id"] == table1.c["source_id"]), isouter=False))
        .where((table1.c["workstream_id"] == bindparam("p0")))
        .order_by(table1.c["created_at"].asc(), table2.c["source_id"].asc())
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": workstream_id})
