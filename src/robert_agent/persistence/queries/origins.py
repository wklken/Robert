"""Shared queries operations for origins."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import (
    select,
    bindparam,
    and_,
    not_,
    null,
    case,
    func,
    LABEL_STYLE_NONE,
)


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_task_context(conn, workstream_id, repo_id, repo_id_3):
    query1 = select(bindparam("p0").label("stream_id"), sql_constant(0).label("depth")).set_label_style(
        LABEL_STYLE_NONE
    )
    cte2 = query1.cte("origins", recursive=True)
    o3 = cte2.alias("o3")
    table4 = tables["workstreams"].alias("w")
    query5 = (
        select(table4.c["origin_workstream_id"], (o3.c["depth"] + sql_constant(1)))
        .select_from(o3.join(table4, (table4.c["workstream_id"] == o3.c["stream_id"]), isouter=False))
        .where(
            and_(
                not_(table4.c["origin_workstream_id"].is_(null())),
                table4.c["repo_id"] == bindparam("p1"),
                o3.c["depth"] < sql_constant(7),
            )
        )
        .correlate_except(o3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    cte2 = cte2.union_all(query5)
    o6 = cte2.alias("o6")
    table7 = tables["work_items"].alias("wi")
    query8 = (
        select(
            table7.c["work_item_id"],
            table7.c["title"],
            table7.c["version"],
            table7.c["origin_type"],
            case(
                (
                    (func.length(table7.c["description"]) <= sql_constant(8000)),
                    table7.c["description"],
                )
            ).label("description"),
        )
        .select_from(o6.join(table7, (table7.c["workstream_id"] == o6.c["stream_id"]), isouter=False))
        .where((table7.c["repo_id"] == bindparam("p2")))
        .order_by(o6.c["depth"].asc())
        .limit(sql_constant(1))
        .correlate_except(o6, table7)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query8, {"p0": workstream_id, "p1": repo_id, "p2": repo_id_3})


def find_for_repair_ancestor(conn, scope_id):
    query1 = (
        select(
            tables["tasks"].c["workstream_id"].label("stream_id"),
            sql_constant(0).label("depth"),
        )
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["task_id"] == bindparam("p0")))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    cte2 = query1.cte("origins", recursive=True)
    o3 = cte2.alias("o3")
    table4 = tables["workstreams"].alias("w")
    query5 = (
        select(table4.c["origin_workstream_id"], (o3.c["depth"] + sql_constant(1)))
        .select_from(o3.join(table4, (table4.c["workstream_id"] == o3.c["stream_id"]), isouter=False))
        .where(
            and_(
                not_(table4.c["origin_workstream_id"].is_(null())),
                (o3.c["depth"] < sql_constant(16)),
            )
        )
        .correlate_except(o3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    cte2 = cte2.union_all(query5)
    o6 = cte2.alias("o6")
    table7 = tables["work_items"].alias("wi")
    query8 = (
        select(
            table7.c["work_item_id"],
            func.json_extract(
                table7.c["metadata_json"],
                sql_constant("$.supervision_repair.incident_id"),
            ),
        )
        .select_from(o6.join(table7, (table7.c["workstream_id"] == o6.c["stream_id"]), isouter=False))
        .where(
            and_(
                (table7.c["created_by"] == sql_constant("system:supervisor")),
                (
                    func.json_type(table7.c["metadata_json"], sql_constant("$.supervision_repair"))
                    == sql_constant("object")
                ),
            )
        )
        .order_by(o6.c["depth"].asc())
        .limit(sql_constant(1))
        .correlate_except(o6, table7)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query8, {"p0": scope_id})
