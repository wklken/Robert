"""Shared queries operations for retries."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import (
    select,
    table,
    column,
    bindparam,
    and_,
    not_,
    null,
    func,
    LABEL_STYLE_NONE,
)


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_execution_recovery(conn, task_id, repo_id, lineage):
    query1 = select(bindparam("p0").label("task_id"), sql_constant(0).label("depth")).set_label_style(LABEL_STYLE_NONE)
    # A named reference lets the row limit apply to the entire recursive union,
    # including the seed, before that union becomes the CTE.
    recursive_rows = table("retries", column("task_id"), column("depth"))
    table3 = tables["tasks"].alias("child")
    table4 = tables["workstreams"].alias("w")
    query5 = (
        select(table3.c["task_id"], (recursive_rows.c["depth"] + sql_constant(1)))
        .select_from(
            recursive_rows.join(
                table3, (table3.c["parent_task_id"] == recursive_rows.c["task_id"]), isouter=False
            ).join(
                table4,
                and_(
                    (table4.c["workstream_id"] == table3.c["workstream_id"]),
                    (table4.c["repo_id"] == bindparam("p1")),
                ),
                isouter=False,
            )
        )
        .where(
            and_(
                (recursive_rows.c["depth"] < sql_constant(16)),
                (
                    func.json_extract(
                        table3.c["metadata_json"],
                        sql_constant("$.supervision_lineage_key"),
                    )
                    == bindparam("p2")
                ),
            )
        )
        .correlate_except(recursive_rows, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    cte2 = query1.union_all(query5).limit(sql_constant(64)).cte("retries", recursive=True)
    table6 = tables["tasks"].alias("t")
    table8 = tables["work_items"].alias("wi")
    query9 = (
        select(sql_constant(1))
        .select_from(table8)
        .where(
            and_(
                (table8.c["workstream_id"] == table6.c["workstream_id"]),
                not_(table8.c["canceled_at"].is_(null())),
            )
        )
        .correlate_except(table8)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query7 = (
        select(
            table6.c["task_id"],
            table6.c["lifecycle"],
            table6.c["expected_output"],
            table6.c["parent_task_id"],
        )
        .select_from(cte2.join(table6, (table6.c["task_id"] == cte2.c["task_id"]), isouter=False))
        .where(
            and_(
                (table6.c["lifecycle"] == sql_constant("completed")),
                not_(query9.exists()),
            )
        )
        .order_by(cte2.c["depth"].desc(), table6.c["task_id"].asc())
        .limit(sql_constant(8))
        .correlate_except(cte2, table6)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query7, {"p0": task_id, "p1": repo_id, "p2": lineage})
