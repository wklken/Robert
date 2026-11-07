"""Shared queries operations for vote_sessions."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, bindparam, and_, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_resume_waiting_vote(conn, workstream_id, source_type, number):
    table1 = tables["vote_sessions"].alias("vs")
    table2 = tables["tasks"].alias("t")
    query3 = (
        select(table1.c["round_id"], table1.c["task_id"])
        .select_from(table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False))
        .where(
            and_(
                table1.c["workstream_id"] == bindparam("p0"),
                table1.c["status"] == sql_constant("waiting_for_user"),
                table2.c["lifecycle"] == sql_constant("waiting_for_user"),
                table1.c["source_type"] == bindparam("p1"),
                table1.c["number"] == bindparam("p2"),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": workstream_id, "p1": source_type, "p2": number})
