"""Shared queries operations for work_items."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import (
    select,
    bindparam,
    and_,
    or_,
    not_,
    null,
    func,
    tuple_,
    LABEL_STYLE_NONE,
)


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_origin_type_by_workstream_id(conn, workstream_id):
    query1 = (
        select(tables["work_items"].c["origin_type"])
        .select_from(tables["work_items"])
        .where((tables["work_items"].c["workstream_id"] == bindparam("p0")))
        .correlate_except(tables["work_items"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": workstream_id})


def record_result_db(conn, task_id):
    query2 = (
        select(tables["tasks"].c["workstream_id"])
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["task_id"] == bindparam("p0")))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    query1 = (
        select(tables["work_items"].c["origin_type"])
        .select_from(tables["work_items"])
        .where((tables["work_items"].c["workstream_id"] == query2.scalar_subquery()))
        .correlate_except(tables["work_items"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_get_work_item_detail(conn, work_item_id):
    table1 = tables["work_items"].alias("wi")
    table2 = tables["repos"].alias("r")
    query3 = (
        select(table1, table2.c["full_name"].label("repo_full_name"))
        .select_from(table1.join(table2, (table2.c["repo_id"] == table1.c["repo_id"]), isouter=False))
        .where((table1.c["work_item_id"] == bindparam("p0")))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": work_item_id})


def list_work_item_events(conn, work_item_id):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["work_items"])
        .where((tables["work_items"].c["work_item_id"] == bindparam("p0")))
        .correlate_except(tables["work_items"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": work_item_id})


def find_for_get_work_item(conn, workstream_id, value_2, workstream_id_3):
    query1 = (
        select(tables["work_items"].c["work_item_id"])
        .select_from(tables["work_items"])
        .where(tables["work_items"].c["workstream_id"].in_([bindparam("p0"), bindparam("p1")]))
        .order_by((tables["work_items"].c["workstream_id"] == bindparam("p2")).desc())
        .limit(sql_constant(1))
        .correlate_except(tables["work_items"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": workstream_id, "p1": value_2, "p2": workstream_id_3})


def find_for_ordinary_dispatch_allowed(conn, task_id):
    table1 = tables["work_items"].alias("wi")
    table2 = tables["tasks"].alias("t")
    query3 = (
        select(table1.c["origin_type"])
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
        )
        .where((table2.c["task_id"] == bindparam("p0")))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": task_id})


def find_record_by_work_item_id(conn, work_item_id):
    query1 = (
        select(tables["work_items"])
        .select_from(tables["work_items"])
        .where((tables["work_items"].c["work_item_id"] == bindparam("p0")))
        .correlate_except(tables["work_items"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": work_item_id})


def find_record_by_origin_source_id(conn, root_source_id):
    query1 = (
        select(tables["work_items"])
        .select_from(tables["work_items"])
        .where((tables["work_items"].c["origin_source_id"] == bindparam("p0")))
        .correlate_except(tables["work_items"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": root_source_id})


def find_for_ensure_github_work_item(conn, root_workstream_id, root_source_id):
    query1 = (
        select(tables["work_items"])
        .select_from(tables["work_items"])
        .where(
            or_(
                (tables["work_items"].c["workstream_id"] == bindparam("p0")),
                (tables["work_items"].c["origin_source_id"] == bindparam("p1")),
            )
        )
        .correlate_except(tables["work_items"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": root_workstream_id, "p1": root_source_id})


def find_record_by_creation_idempotency_key(conn, idempotency_key):
    query1 = (
        select(tables["work_items"])
        .select_from(tables["work_items"])
        .where((tables["work_items"].c["creation_idempotency_key"] == bindparam("p0")))
        .correlate_except(tables["work_items"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": idempotency_key})


def find_record_by_workstream_id(conn, current):
    query1 = (
        select(tables["work_items"])
        .select_from(tables["work_items"])
        .where((tables["work_items"].c["workstream_id"] == bindparam("p0")))
        .correlate_except(tables["work_items"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": current})


def find_for_complete_published_analysis_items(conn, repo_id):
    table1 = tables["work_items"].alias("wi")
    table2 = tables["github_sources"].alias("gs")
    table3 = tables["workstreams"].alias("w")
    table4 = tables["tasks"].alias("t")
    table5 = tables["worker_results"].alias("wr")
    table7 = tables["github_actions"].alias("ga")
    query8 = (
        select(sql_constant(1))
        .select_from(table7)
        .where(
            and_(
                table7.c["result_id"] == table5.c["result_id"],
                table7.c["action_type"] == sql_constant("comment"),
                table7.c["audit_status"] == sql_constant("accepted"),
                table7.c["publish_status"] == sql_constant("published"),
            )
        )
        .correlate_except(table7)
        .set_label_style(LABEL_STYLE_NONE)
    )
    table9 = tables["github_actions"].alias("ga")
    query10 = (
        select(sql_constant(1))
        .select_from(table9)
        .where(
            and_(
                (table9.c["result_id"] == table5.c["result_id"]),
                (
                    or_(
                        (table9.c["audit_status"] != sql_constant("accepted")),
                        (table9.c["publish_status"] != sql_constant("published")),
                    )
                ),
            )
        )
        .correlate_except(table9)
        .set_label_style(LABEL_STYLE_NONE)
    )
    table11 = tables["workstreams"].alias("related")
    table12 = tables["github_sources"].alias("pr")
    query13 = (
        select(sql_constant(1))
        .select_from(
            table11.join(
                table12,
                (table12.c["source_id"] == table11.c["primary_source_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                (
                    or_(
                        (table11.c["workstream_id"] == table3.c["workstream_id"]),
                        (table11.c["origin_workstream_id"] == table3.c["workstream_id"]),
                    )
                ),
                (table12.c["source_type"] == sql_constant("pull_request")),
            )
        )
        .correlate_except(table11, table12)
        .set_label_style(LABEL_STYLE_NONE)
    )
    table14 = tables["tasks"].alias("newer")
    query15 = (
        select(sql_constant(1))
        .select_from(table14)
        .where(
            and_(
                (table14.c["workstream_id"] == table4.c["workstream_id"]),
                (
                    tuple_(table14.c["created_at"], table14.c["task_id"])
                    > tuple_(table4.c["created_at"], table4.c["task_id"])
                ),
            )
        )
        .correlate_except(table14)
        .set_label_style(LABEL_STYLE_NONE)
    )
    table16 = tables["task_events"].alias("te")
    query17 = (
        select(sql_constant(1))
        .select_from(table16)
        .where(
            and_(
                (table16.c["task_id"] == table4.c["task_id"]),
                table16.c["relationship"].in_([sql_constant("pending"), sql_constant("context")]),
            )
        )
        .correlate_except(table16)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query6 = (
        select(
            table1,
            table4.c["task_id"].label("analysis_task_id"),
            table5.c["result_id"].label("analysis_result_id"),
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["source_id"] == table1.c["origin_source_id"]),
                isouter=False,
            )
            .join(
                table3,
                (table3.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
            .join(
                table4,
                (table4.c["workstream_id"] == table3.c["workstream_id"]),
                isouter=False,
            )
            .join(table5, (table5.c["task_id"] == table4.c["task_id"]), isouter=False)
        )
        .where(
            and_(
                table1.c["repo_id"] == bindparam("p0"),
                table1.c["origin_type"] == sql_constant("github"),
                table1.c["completed_at"].is_(null()),
                table1.c["canceled_at"].is_(null()),
                table2.c["source_type"] == sql_constant("issue"),
                func.lower(table2.c["state"]) == sql_constant("open"),
                table3.c["lifecycle"] == sql_constant("completed"),
                table3.c["active_task_id"].is_(null()),
                table4.c["lifecycle"] == sql_constant("completed"),
                table5.c["output_type"] == sql_constant("comment_analysis"),
                func.json_extract(table5.c["metadata_json"], sql_constant("$.audit.status"))
                == sql_constant("accepted"),
                query8.exists(),
                not_(query10.exists()),
                not_(query13.exists()),
                not_(query15.exists()),
                not_(query17.exists()),
            )
        )
        .correlate_except(table1, table2, table3, table4, table5)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query6, {"p0": repo_id})


def find_work_item_id_and_origin_type_by_workstream_id(conn, workstream_id):
    query1 = (
        select(
            tables["work_items"].c["work_item_id"],
            tables["work_items"].c["origin_type"],
        )
        .select_from(tables["work_items"])
        .where((tables["work_items"].c["workstream_id"] == bindparam("p0")))
        .correlate_except(tables["work_items"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": workstream_id})


def find_for_delivery(conn, work_item_id):
    table1 = tables["work_items"].alias("wi")
    table2 = tables["tasks"].alias("t")
    table3 = tables["worker_results"].alias("wr")
    table4 = tables["attempts"].alias("a")
    query5 = (
        select(
            table3.c["result_id"],
            table3.c["task_id"],
            func.json_extract(
                table3.c["metadata_json"],
                sql_constant("$.audit.action_scope.result_head_sha"),
            ).label("revision"),
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
            .join(table3, (table3.c["task_id"] == table2.c["task_id"]), isouter=False)
            .join(
                table4,
                (table4.c["attempt_id"] == table3.c["attempt_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table1.c["work_item_id"] == bindparam("p0"),
                table3.c["output_type"] == sql_constant("new_pr"),
                table4.c["status"] == sql_constant("completed"),
                func.json_extract(table3.c["metadata_json"], sql_constant("$.audit.status"))
                == sql_constant("accepted"),
                or_(
                    func.json_type(table4.c["metadata_json"], sql_constant("$.dispatch.pid")).is_not(
                        sql_constant("integer")
                    ),
                    not_(
                        func.json_extract(
                            table4.c["metadata_json"],
                            sql_constant("$.process_stopped_at"),
                        ).is_(null())
                    ),
                ),
            )
        )
        .order_by(table3.c["created_at"].desc(), table3.c["result_id"].desc())
        .limit(sql_constant(1))
        .correlate_except(table1, table2, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query5, {"p0": work_item_id})


def find_for_verification_owner(conn, item_id, repository):
    table1 = tables["work_items"].alias("wi")
    table2 = tables["repos"].alias("r")
    query3 = (
        select(sql_constant(1))
        .select_from(table1.join(table2, (table2.c["repo_id"] == table1.c["repo_id"]), isouter=False))
        .where(
            and_(
                table1.c["work_item_id"] == bindparam("p0"),
                table1.c["created_by"] == sql_constant("system:supervisor"),
                table1.c["canceled_at"].is_(null()),
                table2.c["enabled"] == sql_constant(1),
                func.lower(table2.c["full_name"]) == bindparam("p1"),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": item_id, "p1": repository})


def find_for_propose(conn, idempotency):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["work_items"])
        .where((tables["work_items"].c["creation_idempotency_key"] == bindparam("p0")))
        .correlate_except(tables["work_items"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": idempotency})
