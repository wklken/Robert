"""Shared queries operations for tasks."""

from robert_agent.foundation.contracts.delivery import AuditStatus, OutputType, PublishStatus
from robert_agent.foundation.contracts.execution import AttemptStatus, BindingStatus
from robert_agent.foundation.contracts.work import TaskKind, TaskLifecycle, WorkstreamLifecycle

from functools import cache

from robert_agent.persistence.expressions import sql_constant, valid_json_or_empty

from robert_agent.persistence.predicates import occupied_attempt, runnable_task

from sqlalchemy import (
    select,
    literal_column,
    column,
    bindparam,
    and_,
    or_,
    not_,
    null,
    case,
    func,
    LABEL_STYLE_NONE,
)


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_evidence(conn):
    table1 = tables["tasks"].alias("t")
    table2 = tables["attempts"].alias("a")
    query3 = (
        select(
            table1.c["task_id"],
            table1.c["lifecycle"],
            table1.c["route_id"],
            table2.c["worktree_path"],
            table2.c["branch_name"],
        )
        .select_from(table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False))
        .order_by(table1.c["created_at"].desc())
        .limit(sql_constant(1))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {})


def find_for_current_evidence(conn):
    table1 = tables["tasks"].alias("t")
    table2 = tables["route_decisions"].alias("rd")
    table3 = tables["attempts"].alias("a")
    table4 = tables["worker_results"].alias("wr")
    query5 = (
        select(
            table1.c["task_id"],
            table1.c["lifecycle"],
            table2.c["route_id"],
            table3.c["attempt_id"],
            table3.c["status"],
            table4.c["result_id"],
            table4.c["output_type"],
            table4.c["metadata_json"],
        )
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=True)
            .join(table3, (table3.c["task_id"] == table1.c["task_id"]), isouter=True)
            .join(table4, (table4.c["attempt_id"] == table3.c["attempt_id"]), isouter=True)
        )
        .order_by(
            table1.c["created_at"].desc(),
            table3.c["attempt_no"].desc(),
            table4.c["created_at"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(table1, table2, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query5, {})


def find_task_kind_and_lifecycle_by_task_id(conn, subject_id):
    query1 = (
        select(tables["tasks"].c["task_kind"], tables["tasks"].c["lifecycle"])
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["task_id"] == bindparam("p0")))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": subject_id})


def find_lifecycle_by_task_id(conn, value_1):
    query1 = (
        select(tables["tasks"].c["lifecycle"])
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["task_id"] == bindparam("p0")))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": value_1})


def find_for_for_result(conn, attempt_id, task_id, workstream_id):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    table3 = tables["repos"].alias("r")
    table4 = tables["attempts"].alias("a")
    query5 = (
        select(
            table3.c["full_name"],
            table3.c["default_base_branch"],
            table4.c["worktree_path"],
            table4.c["branch_name"],
            table4.c["metadata_json"],
            table1.c["task_kind"],
            table1.c["metadata_json"],
            table3.c["github_account"],
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
            .join(table3, (table3.c["repo_id"] == table2.c["repo_id"]), isouter=False)
            .join(
                table4,
                and_(
                    (table4.c["attempt_id"] == bindparam("p0")),
                    (table4.c["task_id"] == table1.c["task_id"]),
                ),
                isouter=False,
            )
        )
        .where(
            and_(
                (table1.c["task_id"] == bindparam("p1")),
                (table1.c["workstream_id"] == bindparam("p2")),
            )
        )
        .correlate_except(table1, table2, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query5, {"p0": attempt_id, "p1": task_id, "p2": workstream_id})


def insert_worker_result_wakeup(conn, task_id):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    table3 = tables["work_items"].alias("wi")
    query4 = (
        select(table2.c["repo_id"], table3.c["work_item_id"])
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            ).join(
                table3,
                (table3.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=True,
            )
        )
        .where((table1.c["task_id"] == bindparam("p0")))
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": task_id})


def find_lifecycle_and_task_kind_and_metadata_json_by_task_id(conn, task_id):
    query1 = (
        select(
            tables["tasks"].c["lifecycle"],
            tables["tasks"].c["task_kind"],
            tables["tasks"].c["metadata_json"],
        )
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["task_id"] == bindparam("p0")))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_context(conn, task_id, attempt_id):
    table1 = tables["tasks"].alias("t")
    table2 = tables["attempts"].alias("a")
    table3 = tables["route_decisions"].alias("rd")
    query4 = (
        select(
            table1.c["workstream_id"],
            table1.c["task_kind"],
            table1.c["metadata_json"],
            table1.c["lifecycle"],
            table2.c["status"],
            table2.c["metadata_json"],
            table3.c["allowed_github_actions_json"],
            table3.c["expected_output"],
            table3.c["required_skills_json"],
            table3.c["recommended_skills_json"],
            table3.c["route_id"],
        )
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False).join(
                table3, (table3.c["task_id"] == table1.c["task_id"]), isouter=False
            )
        )
        .where(
            and_(
                (table1.c["task_id"] == bindparam("p0")),
                (table2.c["attempt_id"] == bindparam("p1")),
            )
        )
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": task_id, "p1": attempt_id})


def find_record_by_task_id(conn, task_id):
    query1 = (
        select(tables["tasks"])
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["task_id"] == bindparam("p0")))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_workstream_id_by_task_id(conn, task_id):
    query1 = (
        select(tables["tasks"].c["workstream_id"])
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["task_id"] == bindparam("p0")))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_metadata_json_and_workstream_id_by_task_id(conn, task_id):
    query1 = (
        select(tables["tasks"].c["metadata_json"], tables["tasks"].c["workstream_id"])
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["task_id"] == bindparam("p0")))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_task_kind_and_metadata_json_by_task_id(conn, task_id):
    query1 = (
        select(tables["tasks"].c["task_kind"], tables["tasks"].c["metadata_json"])
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["task_id"] == bindparam("p0")))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_parent_allows_child(conn, parent_task_id):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["tasks"])
        .where(
            and_(
                (tables["tasks"].c["task_id"] == bindparam("p0")),
                not_(
                    func.json_extract(
                        tables["tasks"].c["metadata_json"],
                        sql_constant("$.execution_attention"),
                    ).is_(null())
                ),
            )
        )
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": parent_task_id})


def find_task_kind_and_lifecycle_and_metadata_json_by_task_id(conn, task_id):
    query1 = (
        select(
            tables["tasks"].c["task_kind"],
            tables["tasks"].c["lifecycle"],
            tables["tasks"].c["metadata_json"],
        )
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["task_id"] == bindparam("p0")))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_metadata_json_by_task_id(conn, task_id):
    query1 = (
        select(tables["tasks"].c["metadata_json"])
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["task_id"] == bindparam("p0")))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_attempt_is_active(conn, task_id, attempt_id):
    table1 = tables["tasks"].alias("t")
    table2 = tables["attempts"].alias("a")
    query4 = (
        select(func.max(tables["attempts"].c["attempt_no"]))
        .select_from(tables["attempts"])
        .where((tables["attempts"].c["task_id"] == table1.c["task_id"]))
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    query3 = (
        select(table1.c["lifecycle"], table2.c["status"])
        .select_from(table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False))
        .where(
            and_(
                table1.c["task_id"] == bindparam("p0"),
                table2.c["attempt_id"] == bindparam("p1"),
                table2.c["attempt_no"] == query4.scalar_subquery(),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": task_id, "p1": attempt_id})


def find_for_build_dashboard_data(conn, history_limit):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    table3 = tables["route_decisions"].alias("rd")
    query4 = (
        select(
            table1.c["task_id"],
            table1.c["workstream_id"],
            table1.c["lifecycle"],
            table1.c["priority"],
            func.coalesce(table1.c["route_id"], table3.c["route_id"]).label("route_id"),
            func.coalesce(table1.c["expected_output"], table3.c["expected_output"]).label("expected_output"),
            table1.c["updated_at"],
            table2.c["lifecycle"].label("workstream_lifecycle"),
            table2.c["repo_id"],
            tables["repos"].c["full_name"].label("repo_full_name"),
            table3.c["route_decision_id"],
            table3.c["confidence"].label("route_confidence"),
            table3.c["allowed_github_actions_json"],
            table3.c["recommended_skills_json"],
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
            .join(
                tables["repos"],
                (tables["repos"].c["repo_id"] == table2.c["repo_id"]),
                isouter=False,
            )
            .join(table3, (table3.c["task_id"] == table1.c["task_id"]), isouter=True)
        )
        .order_by(table1.c["updated_at"].desc(), table1.c["task_id"].desc())
        .limit(bindparam("p0"))
        .correlate_except(table1, table2, tables["repos"], table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": history_limit})


def find_for_summarize(conn, limit):
    query1 = (
        select(tables["tasks"].c["task_id"])
        .select_from(tables["tasks"])
        .order_by(tables["tasks"].c["created_at"].desc(), tables["tasks"].c["task_id"].desc())
        .limit(bindparam("p0"))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": limit})


def find_for_build_workstream(conn, workstream_id):
    table1 = tables["tasks"].alias("t")
    table2 = tables["attempts"].alias("a")
    query3 = (
        select(tables["attempts"].c["attempt_id"])
        .select_from(tables["attempts"])
        .where((tables["attempts"].c["task_id"] == table1.c["task_id"]))
        .order_by(
            tables["attempts"].c["attempt_no"].desc(),
            tables["attempts"].c["started_at"].desc(),
            tables["attempts"].c["attempt_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    query4 = (
        select(
            table1.c["task_id"],
            table1.c["lifecycle"],
            table1.c["priority"],
            table1.c["route_id"],
            table1.c["expected_output"],
            table1.c["parent_task_id"],
            table1.c["created_at"],
            table1.c["updated_at"],
            table2.c["attempt_id"].label("latest_attempt_id"),
            table2.c["status"].label("latest_attempt_status"),
            table2.c["heartbeat_at"].label("latest_attempt_heartbeat_at"),
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["attempt_id"] == query3.scalar_subquery()),
                isouter=True,
            )
        )
        .where((table1.c["workstream_id"] == bindparam("p0")))
        .order_by(table1.c["updated_at"].desc(), table1.c["task_id"].desc())
        .limit(sql_constant(20))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": workstream_id})


def find_for_build_task(conn, task_id):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    table3 = tables["route_decisions"].alias("rd")
    query4 = (
        select(
            table1.c["task_id"],
            table1.c["workstream_id"],
            table1.c["lifecycle"],
            table1.c["parent_task_id"],
            table1.c["priority"],
            func.coalesce(table1.c["route_id"], table3.c["route_id"]).label("route_id"),
            func.coalesce(table1.c["expected_output"], table3.c["expected_output"]).label("expected_output"),
            table1.c["created_at"],
            table1.c["updated_at"],
            table2.c["lifecycle"].label("workstream_lifecycle"),
            table3.c["confidence"].label("route_confidence"),
            table3.c["allowed_github_actions_json"],
            table3.c["recommended_skills_json"],
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            ).join(table3, (table3.c["task_id"] == table1.c["task_id"]), isouter=True)
        )
        .where((table1.c["task_id"] == bindparam("p0")))
        .order_by(table3.c["created_at"].desc())
        .limit(sql_constant(1))
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": task_id})


def find_for_target(conn, task_id):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["parent_task_id"] == bindparam("p0")))
        .limit(sql_constant(1))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_verified(conn, child, task_id, value_3):
    table1 = tables["tasks"].alias("t")
    table2 = tables["task_events"].alias("te")
    query3 = (
        select(table2.c["event_id"], table2.c["created_at"])
        .select_from(table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False))
        .where(
            and_(
                table1.c["task_id"] == bindparam("p0"),
                table1.c["parent_task_id"] == bindparam("p1"),
                table1.c["workstream_id"] == bindparam("p2"),
                not_(
                    table1.c["lifecycle"].in_(
                        [sql_constant(TaskLifecycle.CANCELED.value), sql_constant(TaskLifecycle.IGNORED.value)]
                    )
                ),
                table2.c["relationship"] == sql_constant("trigger"),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": child, "p1": task_id, "p2": value_3})


def find_for_is_current(conn, successor_task_id, lineage_key):
    query1 = (
        select(
            tables["tasks"].c["task_id"],
            tables["tasks"].c["lifecycle"],
            tables["tasks"].c["expected_output"],
            tables["tasks"].c["parent_task_id"],
        )
        .select_from(tables["tasks"])
        .where(
            and_(
                tables["tasks"].c["task_id"] == bindparam("p0"),
                tables["tasks"].c["lifecycle"] == sql_constant(TaskLifecycle.COMPLETED.value),
                func.json_extract(
                    tables["tasks"].c["metadata_json"],
                    sql_constant("$.supervision_lineage_key"),
                )
                == bindparam("p1"),
            )
        )
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": successor_task_id, "p1": lineage_key})


def find_lifecycle_and_updated_at_by_task_id(conn, scope):
    query1 = (
        select(tables["tasks"].c["lifecycle"], tables["tasks"].c["updated_at"])
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["task_id"] == bindparam("p0")))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": scope})


def find_for_task_facts(conn, task_id, value_2):
    query1 = (
        select(tables["tasks"].c["task_id"], tables["tasks"].c["created_at"])
        .select_from(tables["tasks"])
        .where(
            and_(
                tables["tasks"].c["parent_task_id"] == bindparam("p0"),
                tables["tasks"].c["task_id"] == bindparam("p1"),
                not_(
                    tables["tasks"]
                    .c["lifecycle"]
                    .in_([sql_constant(TaskLifecycle.CANCELED.value), sql_constant(TaskLifecycle.IGNORED.value)])
                ),
            )
        )
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id, "p1": value_2})


def find_for_is_current_2(conn, scope):
    table1 = tables["tasks"].alias("t")
    table3 = tables["work_items"].alias("wi")
    query4 = (
        select(sql_constant(1))
        .select_from(table3)
        .where(
            and_(
                (table3.c["workstream_id"] == table1.c["workstream_id"]),
                not_(table3.c["canceled_at"].is_(null())),
            )
        )
        .correlate_except(table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query2 = (
        select(table1.c["task_id"], table1.c["lifecycle"], table1.c["expected_output"])
        .select_from(table1)
        .where(
            and_(
                table1.c["task_id"] == bindparam("p0"),
                table1.c["lifecycle"] == sql_constant(TaskLifecycle.COMPLETED.value),
                not_(query4.exists()),
            )
        )
        .correlate_except(table1)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query2, {"p0": scope})


def find_for_receipt_facts(conn, target_id, value_2):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    query3 = (
        select(
            table1.c["lifecycle"],
            table2.c["active_task_id"],
            table2.c["lifecycle"].label("stream_lifecycle"),
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                (table1.c["task_id"] == bindparam("p0")),
                (table2.c["repo_id"] == bindparam("p1")),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": target_id, "p1": value_2})


def find_for_guard(conn, task_id):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    table3 = tables["repos"].alias("r")
    query4 = (
        select(table3.c["full_name"], table1.c["task_kind"], table1.c["route_id"])
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            ).join(table3, (table3.c["repo_id"] == table2.c["repo_id"]), isouter=False)
        )
        .where((table1.c["task_id"] == bindparam("p0")))
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": task_id})


def find_for_task_context(conn, scope_id):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    table3 = tables["repos"].alias("r")
    query4 = (
        select(tables["route_decisions"].c["allowed_github_actions_json"])
        .select_from(tables["route_decisions"])
        .where((tables["route_decisions"].c["task_id"] == table1.c["task_id"]))
        .order_by(
            tables["route_decisions"].c["created_at"].desc(),
            tables["route_decisions"].c["route_decision_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(tables["route_decisions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    query5 = (
        select(
            table1.c["task_id"],
            table1.c["lifecycle"],
            table1.c["route_id"],
            table1.c["expected_output"],
            table1.c["workstream_id"],
            table1.c["updated_at"],
            table2.c["repo_id"],
            table3.c["full_name"].label("repository"),
            table3.c["enabled"].label("repository_enabled"),
            query4.scalar_subquery().label("route_actions"),
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            ).join(table3, (table3.c["repo_id"] == table2.c["repo_id"]), isouter=False)
        )
        .where((table1.c["task_id"] == bindparam("p0")))
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query5, {"p0": scope_id})


def find_for_get_task_lineage(conn, task_id):
    query1 = (
        select(
            func.json_extract(
                tables["tasks"].c["metadata_json"],
                sql_constant("$.supervision_lineage_key"),
            )
        )
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["task_id"] == bindparam("p0")))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_repair_task(conn, task_id):
    query1 = (
        select(func.json_extract(tables["tasks"].c["metadata_json"], sql_constant("$.supervision_repair")))
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["task_id"] == bindparam("p0")))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_record_by_workstream_id(conn, workstream_id):
    query1 = (
        select(tables["tasks"])
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["workstream_id"] == bindparam("p0")))
        .order_by(tables["tasks"].c["created_at"].desc(), tables["tasks"].c["task_id"].desc())
        .limit(sql_constant(1))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": workstream_id})


def find_for_retrigger_failed_github_task(conn, task_id):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    table3 = tables["repos"].alias("r")
    query4 = (
        select(
            table1.c["metadata_json"],
            table3.c["full_name"],
            table3.c["github_account"],
            table2.c["repo_id"],
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            ).join(table3, (table3.c["repo_id"] == table2.c["repo_id"]), isouter=False)
        )
        .where((table1.c["task_id"] == bindparam("p0")))
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": task_id})


def find_for_retrigger_failed_github_task_2(conn, task_id):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    table3 = tables["github_sources"].alias("gs")
    query4 = (
        select(
            table1.c["task_id"],
            table1.c["lifecycle"],
            table1.c["workstream_id"],
            table2.c["repo_id"],
            table2.c["lifecycle"].label("workstream_lifecycle"),
            table2.c["active_task_id"],
            table3.c["state"].label("source_state"),
            table1.c["metadata_json"],
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            ).join(
                table3,
                (table3.c["source_id"] == table2.c["primary_source_id"]),
                isouter=False,
            )
        )
        .where((table1.c["task_id"] == bindparam("p0")))
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": task_id})


def find_task_id_by_workstream_id(conn, workstream_id):
    query1 = (
        select(tables["tasks"].c["task_id"])
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["workstream_id"] == bindparam("p0")))
        .order_by(tables["tasks"].c["created_at"].desc(), tables["tasks"].c["task_id"].desc())
        .limit(sql_constant(1))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": workstream_id})


def find_for_pending_handoff_current(conn, task_id, workstream_id):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    table3 = tables["work_items"].alias("wi")
    table4 = tables["github_sources"].alias("gs")
    table6 = tables["tasks"].alias("child")
    query7 = (
        select(sql_constant(1))
        .select_from(table6)
        .where((table6.c["parent_task_id"] == table1.c["task_id"]))
        .correlate_except(table6)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query5 = (
        select(sql_constant(1))
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
            .join(
                table3,
                (table3.c["workstream_id"] == table2.c["workstream_id"]),
                isouter=True,
            )
            .join(
                table4,
                (table4.c["source_id"] == table2.c["primary_source_id"]),
                isouter=True,
            )
        )
        .where(
            and_(
                table1.c["task_id"] == bindparam("p0"),
                table1.c["workstream_id"] == bindparam("p1"),
                table1.c["lifecycle"] == sql_constant(TaskLifecycle.COMPLETED.value),
                table2.c["lifecycle"] == sql_constant(WorkstreamLifecycle.ACTIVE.value),
                table2.c["active_task_id"] == table1.c["task_id"],
                table3.c["canceled_at"].is_(null()),
                func.coalesce(func.lower(table4.c["state"]), sql_constant("open")) != sql_constant("closed"),
                not_(query7.exists()),
            )
        )
        .correlate_except(table1, table2, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query5, {"p0": task_id, "p1": workstream_id})


def find_for_recover_orphaned_classification_results(conn, task_id):
    query1 = (
        select(func.count(literal_column("*")))
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["parent_task_id"] == bindparam("p0")))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_create_task_attempt_and_prompt(conn, existing_task_id, workstream_id):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    table4 = tables["attempts"].alias("a")
    query5 = (
        select(sql_constant(1))
        .select_from(table4)
        .where((table4.c["task_id"] == table1.c["task_id"]))
        .correlate_except(table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query3 = (
        select(table1.c["metadata_json"])
        .select_from(
            table1.join(
                table2,
                (table2.c["active_task_id"] == table1.c["task_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table1.c["task_id"] == bindparam("p0"),
                table1.c["workstream_id"] == bindparam("p1"),
                table1.c["lifecycle"] == sql_constant(TaskLifecycle.DETECTED.value),
                not_(query5.exists()),
                not_(
                    func.json_extract(
                        table1.c["metadata_json"],
                        sql_constant("$.trigger_work_item_event_id"),
                    ).is_(null())
                ),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": existing_task_id, "p1": workstream_id})


def find_for_prepare_detected_local_tasks(conn, repo_id):
    table1 = tables["tasks"].alias("t")
    table2 = tables["work_items"].alias("wi")
    table3 = tables["workstreams"].alias("w")
    table5 = tables["attempts"].alias("a")
    query6 = (
        select(sql_constant(1))
        .select_from(table5)
        .where((table5.c["task_id"] == table1.c["task_id"]))
        .correlate_except(table5)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query4 = (
        select(
            table1.c["task_id"],
            table1.c["workstream_id"],
            table1.c["parent_task_id"],
            table1.c["priority"],
            table1.c["routing_mode"],
            table1.c["requested_worker"],
            table1.c["metadata_json"],
            table2.c["work_item_id"],
            table2.c["title"],
            table2.c["description"],
            table2.c["created_by"],
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            ).join(
                table3,
                (table3.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table3.c["repo_id"] == bindparam("p0"),
                table2.c["origin_type"] == sql_constant("web"),
                table2.c["canceled_at"].is_(null()),
                table2.c["completed_at"].is_(null()),
                table1.c["lifecycle"] == sql_constant(TaskLifecycle.DETECTED.value),
                not_(query6.exists()),
            )
        )
        .order_by(table1.c["created_at"].asc(), table1.c["task_id"].asc())
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": repo_id})


def find_route_id_by_task_id(conn, parent_task_id):
    query1 = (
        select(tables["tasks"].c["route_id"])
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["task_id"] == bindparam("p0")))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": parent_task_id})


def find_parent_task_id_by_task_id(conn, context_task):
    query1 = (
        select(tables["tasks"].c["parent_task_id"])
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["task_id"] == bindparam("p0")))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": context_task})


def find_for_collect_pr_remediation_decisions(conn, workstream_id):
    table1 = tables["tasks"].alias("t")
    table2 = tables["worker_results"].alias("wr")
    query3 = (
        select(sql_constant(1))
        .select_from(table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False))
        .where(
            and_(
                table1.c["workstream_id"] == bindparam("p0"),
                table1.c["task_kind"].in_(
                    [
                        sql_constant(TaskKind.CI_REMEDIATION.value),
                        sql_constant(TaskKind.MERGE_CONFLICT_REMEDIATION.value),
                    ]
                ),
                table1.c["lifecycle"].in_(
                    [
                        sql_constant(TaskLifecycle.QUEUED.value),
                        sql_constant(TaskLifecycle.RUNNING.value),
                        sql_constant(TaskLifecycle.WAITING_FOR_USER.value),
                    ]
                ),
                table2.c["output_type"] == sql_constant(OutputType.WAITING_FOR_USER.value),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": workstream_id})


def find_for_active_task_state(conn, workstream_id):
    table1 = tables["tasks"].alias("t")
    table2 = tables["attempts"].alias("a")
    query3 = (
        select(
            table1.c["task_id"],
            table1.c["lifecycle"],
            table1.c["route_id"],
            table1.c["expected_output"],
            table2.c["attempt_id"],
            table2.c["status"],
        )
        .select_from(table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=True))
        .where(
            and_(
                (table1.c["workstream_id"] == bindparam("p0")),
                table1.c["lifecycle"].in_(
                    [
                        sql_constant(TaskLifecycle.DETECTED.value),
                        sql_constant(TaskLifecycle.AUTHORIZED.value),
                        sql_constant(TaskLifecycle.CLASSIFIED.value),
                        sql_constant(TaskLifecycle.QUEUED.value),
                        sql_constant(TaskLifecycle.RUNNING.value),
                    ]
                ),
            )
        )
        .order_by(table1.c["created_at"].desc(), table2.c["attempt_no"].desc())
        .limit(sql_constant(1))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": workstream_id})


def find_for_github_followup_allows_new_subject(conn, parent_task_id, workstream_id, value_3):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    query3 = (
        select(
            table1.c["lifecycle"],
            table2.c["lifecycle"],
            table2.c["active_task_id"],
            table1.c["metadata_json"],
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table1.c["task_id"] == bindparam("p0"),
                table2.c["workstream_id"] == bindparam("p1"),
                table2.c["repo_id"] == bindparam("p2"),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": parent_task_id, "p1": workstream_id, "p2": value_3})


def record_remote_terminal_state_locked(conn, workstream_id, workstream_id_2):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    query3 = (
        select(sql_constant(1))
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                (
                    or_(
                        (table2.c["workstream_id"] == bindparam("p0")),
                        (table2.c["origin_workstream_id"] == bindparam("p1")),
                    )
                ),
                not_(
                    table1.c["lifecycle"].in_(
                        [
                            sql_constant(TaskLifecycle.COMPLETED.value),
                            sql_constant(TaskLifecycle.FAILED.value),
                            sql_constant(TaskLifecycle.CANCELED.value),
                            sql_constant(TaskLifecycle.IGNORED.value),
                        ]
                    )
                ),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": workstream_id, "p1": workstream_id_2})


def find_task_id_by_workstream_id_2(conn, workstream_id):
    query1 = (
        select(tables["tasks"].c["task_id"])
        .select_from(tables["tasks"])
        .where((tables["tasks"].c["workstream_id"] == bindparam("p0")))
        .order_by(tables["tasks"].c["created_at"].desc())
        .limit(sql_constant(1))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": workstream_id})


def find_for_active_task_id(conn, workstream_id):
    query1 = (
        select(tables["tasks"].c["task_id"])
        .select_from(tables["tasks"])
        .where(
            and_(
                (tables["tasks"].c["workstream_id"] == bindparam("p0")),
                tables["tasks"]
                .c["lifecycle"]
                .in_(
                    [
                        sql_constant(TaskLifecycle.DETECTED.value),
                        sql_constant(TaskLifecycle.AUTHORIZED.value),
                        sql_constant(TaskLifecycle.CLASSIFIED.value),
                        sql_constant(TaskLifecycle.QUEUED.value),
                        sql_constant(TaskLifecycle.RUNNING.value),
                    ]
                ),
            )
        )
        .order_by(tables["tasks"].c["created_at"].desc())
        .limit(sql_constant(1))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": workstream_id})


def mark_repository_busy(conn, repo_id):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    query3 = (
        select(table1.c["task_id"])
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                (table2.c["repo_id"] == bindparam("p0")),
                table1.c["lifecycle"].in_(
                    [sql_constant(TaskLifecycle.QUEUED.value), sql_constant(TaskLifecycle.RUNNING.value)]
                ),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": repo_id})


def find_for_repository_task_ids(conn, repo_id):
    query2 = (
        select(tables["workstreams"].c["workstream_id"])
        .select_from(tables["workstreams"])
        .where((tables["workstreams"].c["repo_id"] == bindparam("p0")))
        .correlate_except(tables["workstreams"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    query1 = (
        select(tables["tasks"].c["task_id"])
        .select_from(tables["tasks"])
        .where(
            and_(
                tables["tasks"].c["workstream_id"].in_(query2),
                tables["tasks"]
                .c["lifecycle"]
                .in_([sql_constant(TaskLifecycle.QUEUED.value), sql_constant(TaskLifecycle.RUNNING.value)]),
            )
        )
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id})


def find_for_authorized_repair(conn, task_id, repo_id):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    table3 = tables["repos"].alias("r")
    table4 = tables["work_items"].alias("wi")
    query5 = (
        select(
            table1.c["route_id"],
            table1.c["expected_output"],
            table1.c["routing_mode"],
            table1.c["requested_worker"],
            table3.c["full_name"],
            table4.c["created_by"],
            table4.c["canceled_at"],
            table4.c["metadata_json"],
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
            .join(table3, (table3.c["repo_id"] == table2.c["repo_id"]), isouter=False)
            .join(
                table4,
                (table4.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table1.c["task_id"] == bindparam("p0"),
                table3.c["repo_id"] == bindparam("p1"),
                table3.c["enabled"] == sql_constant(1),
                not_(
                    table1.c["lifecycle"].in_(
                        [sql_constant(TaskLifecycle.CANCELED.value), sql_constant(TaskLifecycle.IGNORED.value)]
                    )
                ),
            )
        )
        .correlate_except(table1, table2, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query5, {"p0": task_id, "p1": repo_id})


def find_for_original_progress(conn, scope_id):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    query3 = (
        select(sql_constant(1))
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                (table1.c["task_id"] == bindparam("p0")),
                (
                    or_(
                        table1.c["lifecycle"].in_(
                            [sql_constant(TaskLifecycle.CANCELED.value), sql_constant(TaskLifecycle.IGNORED.value)]
                        ),
                        (table2.c["lifecycle"] == sql_constant(WorkstreamLifecycle.CANCELED.value)),
                    )
                ),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": scope_id})


def find_for_prepare_publication_intent(conn, task_id):
    table1 = tables["tasks"].alias("t")
    query2 = (
        select(sql_constant(1))
        .select_from(table1)
        .where(
            and_(
                (table1.c["task_id"] == bindparam("p0")),
                (runnable_task(table1)),
            )
        )
        .correlate_except(table1)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query2, {"p0": task_id})


def find_for_task(conn, task_id):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    table3 = tables["repos"].alias("r")
    table4 = tables["github_sources"].alias("s")
    query7 = (
        select(table1.c["workstream_id"].label("id"), sql_constant(0).label("depth"))
        .where(table1.c.task_id == bindparam("p0"))
        .set_label_style(LABEL_STYLE_NONE)
    )
    cte8 = query7.cte("ancestry", recursive=True)
    table9 = tables["workstreams"].alias("w")
    query10 = (
        select(table9.c["origin_workstream_id"], (column("depth") + sql_constant(1)))
        .select_from(cte8.join(table9, (table9.c["workstream_id"] == cte8.c["id"]), isouter=False))
        .where(
            and_(
                not_(table9.c["origin_workstream_id"].is_(null())),
                (column("depth") < sql_constant(64)),
            )
        )
        .correlate_except(cte8, table9)
        .set_label_style(LABEL_STYLE_NONE)
    )
    cte8 = cte8.union_all(query10)
    table11 = tables["work_items"].alias("wi")
    query12 = (
        select(sql_constant(1))
        .select_from(cte8.join(table11, (table11.c["workstream_id"] == cte8.c["id"]), isouter=False))
        .where(not_(table11.c["canceled_at"].is_(null())))
        .correlate_except(cte8, table11)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query13 = (
        select(
            table1.c["task_id"],
            table1.c["lifecycle"],
            table1.c["workstream_id"],
            table1.c["metadata_json"],
            func.json_extract(table1.c["metadata_json"], sql_constant("$.supervision_lineage_key")).label(
                "lineage_key"
            ),
            table2.c["active_task_id"],
            table2.c["lifecycle"].label("stream_lifecycle"),
            table3.c["full_name"].label("repo"),
            table3.c["enabled"],
            func.coalesce(func.lower(table4.c["state"]), sql_constant("open")).label("source_state"),
            (runnable_task(table1)).label("runnable"),
            query12.exists().label("canceled"),
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
            .join(table3, (table3.c["repo_id"] == table2.c["repo_id"]), isouter=False)
            .join(
                table4,
                (table4.c["source_id"] == table2.c["primary_source_id"]),
                isouter=True,
            )
        )
        .where((table1.c["task_id"] == bindparam("p0")))
        .correlate_except(table1, table2, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query13, {"p0": task_id})


def collect_filtered(conn, items, value_1, value_2, value_3):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    attempt = tables["attempts"].alias("active_attempt")
    active_attempt = (
        select(sql_constant(1))
        .select_from(attempt)
        .where(
            and_(
                attempt.c.task_id == table1.c.task_id,
                occupied_attempt(attempt),
            )
        )
        .correlate_except(attempt)
    )
    table6 = tables["github_actions"].alias("ga")
    query7 = (
        select(sql_constant(1))
        .select_from(table6)
        .where(
            and_(
                table6.c["task_id"] == table1.c["task_id"],
                table6.c["audit_status"] == sql_constant(AuditStatus.ACCEPTED.value),
                table6.c["publish_status"] == sql_constant(PublishStatus.NOT_PUBLISHED.value),
                func.julianday(table6.c["created_at"])
                < func.julianday(bindparam("p1")) - sql_constant(2.0) / sql_constant(1440),
            )
        )
        .correlate_except(table6)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query5 = (
        select(
            table1.c["task_id"],
            case(
                (query7.exists(), sql_constant("publication_delayed")),
                else_=sql_constant("queue_delayed"),
            ),
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table2.c["repo_id"].in_(bindparam("items", expanding=True)),
                table1.c["lifecycle"].in_(
                    [sql_constant(TaskLifecycle.QUEUED.value), sql_constant(TaskLifecycle.RUNNING.value)]
                ),
                or_(
                    and_(
                        table1.c["lifecycle"] == sql_constant(TaskLifecycle.QUEUED.value),
                        not_(active_attempt.exists()),
                        func.julianday(table1.c["created_at"])
                        < func.julianday(bindparam("p0")) - sql_constant(5.0) / sql_constant(1440),
                    ),
                    query7.exists(),
                ),
            )
        )
        .order_by(table1.c["created_at"].asc())
        .limit(bindparam("p2"))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query5, {"p0": value_1, "p1": value_2, "p2": value_3, "items": items})


def cancel_execution_filtered(conn, items):
    query1 = (
        select(tables["tasks"].c["task_id"])
        .select_from(tables["tasks"])
        .where(tables["tasks"].c["workstream_id"].in_(bindparam("items", expanding=True)))
        .order_by(tables["tasks"].c["task_id"].asc())
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"items": items})


@cache
def _ready_rows_filtered_statement():
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    table3 = tables["attempts"].alias("a")
    table4 = tables["worker_results"].alias("wr")
    table6 = tables["work_items"].alias("wi")
    query7 = (
        select(sql_constant(1))
        .select_from(table6)
        .where(
            and_(
                (table6.c["workstream_id"] == table2.c["workstream_id"]),
                not_(table6.c["canceled_at"].is_(null())),
            )
        )
        .correlate_except(table6)
        .set_label_style(LABEL_STYLE_NONE)
    )
    table8 = tables["github_sources"].alias("gs")
    query9 = (
        select(sql_constant(1))
        .select_from(table8)
        .where(
            and_(
                (table8.c["source_id"] == table2.c["primary_source_id"]),
                (func.lower(table8.c["state"]) == sql_constant("closed")),
            )
        )
        .correlate_except(table8)
        .set_label_style(LABEL_STYLE_NONE)
    )
    table10 = tables["task_events"].alias("te")
    query11 = (
        select(sql_constant(1))
        .select_from(table10)
        .where(
            and_(
                (table10.c["task_id"] == table1.c["task_id"]),
                table10.c["relationship"].in_([sql_constant("context"), sql_constant("pending")]),
            )
        )
        .correlate_except(table10)
        .set_label_style(LABEL_STYLE_NONE)
    )
    table12 = tables["tasks"].alias("child")
    query13 = (
        select(sql_constant(1))
        .select_from(table12)
        .where((table12.c["parent_task_id"] == table1.c["task_id"]))
        .correlate_except(table12)
        .set_label_style(LABEL_STYLE_NONE)
    )
    table14 = tables["github_actions"].alias("ga")
    query15 = (
        select(sql_constant(1))
        .select_from(table14)
        .where(
            and_(
                (table14.c["result_id"] == table4.c["result_id"]),
                (
                    or_(
                        (table14.c["audit_status"] != sql_constant(AuditStatus.ACCEPTED.value)),
                        (table14.c["publish_status"] != sql_constant(PublishStatus.PUBLISHED.value)),
                    )
                ),
            )
        )
        .correlate_except(table14)
        .set_label_style(LABEL_STYLE_NONE)
    )
    table16 = tables["execution_subjects"].alias("owner")
    query17 = (
        select(sql_constant(1))
        .select_from(table16)
        .where(
            and_(
                (table16.c["binding_status"] == sql_constant(BindingStatus.ACTIVE.value)),
                (
                    or_(
                        (
                            and_(
                                table1.c["task_kind"] == sql_constant(TaskKind.HUMAN_REQUEST.value),
                                table16.c["subject_type"] == sql_constant("task"),
                                table16.c["subject_id"] == table1.c["task_id"],
                            )
                        ),
                        (
                            and_(
                                table1.c["task_kind"].in_(
                                    [
                                        sql_constant(TaskKind.CI_REMEDIATION.value),
                                        sql_constant(TaskKind.MERGE_CONFLICT_REMEDIATION.value),
                                    ]
                                ),
                                table16.c["subject_type"] == sql_constant("remediation_episode"),
                                table16.c["subject_id"]
                                == func.json_extract(
                                    table1.c["metadata_json"],
                                    sql_constant("$.remediation_episode_id"),
                                ),
                            )
                        ),
                    )
                ),
            )
        )
        .correlate_except(table16)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query5 = (
        select(
            table1.c["task_id"],
            table1.c["workstream_id"],
            func.json_extract(
                table1.c["metadata_json"],
                sql_constant("$.pending_handoff.attempt_count"),
            ),
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
            .join(table3, (table3.c["task_id"] == table1.c["task_id"]), isouter=False)
            .join(
                table4,
                (table4.c["attempt_id"] == table3.c["attempt_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table2.c["repo_id"].in_(bindparam("items", expanding=True)),
                table2.c["lifecycle"] == sql_constant(WorkstreamLifecycle.ACTIVE.value),
                table2.c["active_task_id"] == table1.c["task_id"],
                table1.c["lifecycle"] == sql_constant(TaskLifecycle.COMPLETED.value),
                not_(query7.exists()),
                not_(query9.exists()),
                table3.c["status"] == sql_constant(AttemptStatus.COMPLETED.value),
                not_(
                    table4.c["output_type"].in_(
                        [
                            sql_constant(OutputType.CLASSIFICATION_RESULT.value),
                            sql_constant(OutputType.WAITING_FOR_USER.value),
                        ]
                    )
                ),
                func.json_extract(table4.c["metadata_json"], sql_constant("$.audit.status"))
                == sql_constant("accepted"),
                func.json_extract(table4.c["metadata_json"], sql_constant("$.result_contract")) == bindparam("p0"),
                or_(
                    func.json_type(table3.c["metadata_json"], sql_constant("$.dispatch.pid")).is_not(
                        sql_constant("integer")
                    ),
                    not_(
                        func.json_extract(
                            table3.c["metadata_json"],
                            sql_constant("$.process_stopped_at"),
                        ).is_(null())
                    ),
                ),
                func.coalesce(
                    func.json_extract(
                        table1.c["metadata_json"],
                        sql_constant("$.pending_handoff.retryable"),
                    ),
                    sql_constant(1),
                )
                != sql_constant(0),
                or_(
                    func.json_extract(
                        table1.c["metadata_json"],
                        sql_constant("$.pending_handoff.next_retry_at"),
                    ).is_(null()),
                    func.julianday(
                        func.json_extract(
                            table1.c["metadata_json"],
                            sql_constant("$.pending_handoff.next_retry_at"),
                        )
                    )
                    <= func.julianday(bindparam("p1")),
                ),
                query11.exists(),
                not_(query13.exists()),
                not_(query15.exists()),
                query17.exists(),
                runnable_task(table1),
            )
        )
        .order_by(table1.c["created_at"].asc(), table1.c["task_id"].asc())
        .correlate_except(table1, table2, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return query5


def ready_rows_filtered(conn, items, value_1, value_2):
    return statements.read(conn, _ready_rows_filtered_statement(), {"p0": value_1, "p1": value_2, "items": items})


def github_followup_allows_new_subject_filtered(conn, items):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["tasks"])
        .where(
            and_(
                tables["tasks"].c["workstream_id"].in_(bindparam("items", expanding=True)),
                not_(
                    tables["tasks"]
                    .c["lifecycle"]
                    .in_(
                        [
                            sql_constant(TaskLifecycle.COMPLETED.value),
                            sql_constant(TaskLifecycle.FAILED.value),
                            sql_constant(TaskLifecycle.CANCELED.value),
                            sql_constant(TaskLifecycle.IGNORED.value),
                        ]
                    )
                ),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"items": items})


@cache
def _current_task_filtered_statement():
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    table4 = tables["attempts"].alias("a")
    query5 = (
        select(sql_constant(1))
        .select_from(table4)
        .where(
            and_(
                (table4.c["task_id"] == table1.c["task_id"]),
                table4.c["status"].in_(
                    [sql_constant(AttemptStatus.RUNNING.value), sql_constant(AttemptStatus.STALE.value)]
                ),
            )
        )
        .correlate_except(table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query3 = (
        select(table1)
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(table1.c["workstream_id"].in_(bindparam("items", expanding=True)))
        .order_by(
            query5.exists().desc(),
            (table1.c["task_id"] == table2.c["active_task_id"]).desc(),
            table1.c["created_at"].desc(),
            table1.c["task_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return query3


def current_task_filtered(conn, items):
    return statements.read(conn, _current_task_filtered_statement(), {"items": items})


@cache
def _lifecycle_facts_filtered_statement():
    query1 = (
        select(tables["tasks"].c["metadata_json"])
        .select_from(tables["tasks"])
        .where(tables["tasks"].c["workstream_id"].in_(bindparam("items", expanding=True)))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return query1


def lifecycle_facts_filtered(conn, items):
    return statements.read(conn, _lifecycle_facts_filtered_statement(), {"items": items})


@cache
def _lifecycle_facts_filtered_2_statement():
    query1 = (
        select(
            tables["tasks"].c["task_id"],
            func.json_extract(
                valid_json_or_empty(tables["tasks"].c.metadata_json),
                sql_constant("$.remediation_episode_id"),
            ),
        )
        .select_from(tables["tasks"])
        .where(tables["tasks"].c["workstream_id"].in_(bindparam("items", expanding=True)))
        .correlate_except(tables["tasks"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return query1


def lifecycle_facts_filtered_2(conn, items):
    return statements.read(conn, _lifecycle_facts_filtered_2_statement(), {"items": items})


def publication_actor(conn, task_id):
    task = tables["tasks"]
    stream = tables["workstreams"]
    repo = tables["repos"]
    return statements.read(
        conn,
        select(repo.c.github_account)
        .select_from(
            task.join(stream, stream.c.workstream_id == task.c.workstream_id).join(
                repo, repo.c.repo_id == stream.c.repo_id
            )
        )
        .where(task.c.task_id == task_id),
        {},
    )
