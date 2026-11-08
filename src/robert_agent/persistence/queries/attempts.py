"""Shared queries operations for attempts."""

from robert_agent.foundation.contracts.execution import AttemptStatus, BindingStatus
from robert_agent.foundation.contracts.work import TaskLifecycle, WorkstreamLifecycle

from functools import lru_cache

from functools import cache

from robert_agent.persistence.expressions import sql_constant

from robert_agent.persistence.predicates import occupied_attempt, runnable_task

from sqlalchemy import (
    select,
    literal_column,
    bindparam,
    and_,
    or_,
    not_,
    null,
    case,
    func,
    cast,
    Integer,
    LABEL_STYLE_NONE,
)


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_control_plane_state_gate(conn):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    query3 = (
        select(func.count(literal_column("*")))
        .select_from(table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False))
        .where(
            and_(
                (table1.c["status"] == sql_constant(AttemptStatus.PREPARED.value)),
                table2.c["lifecycle"].in_(
                    [sql_constant(TaskLifecycle.QUEUED.value), sql_constant(TaskLifecycle.RUNNING.value)]
                ),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {})


def find_for_control_plane_state_gate_2(conn):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    query3 = (
        select(func.count(literal_column("*")))
        .select_from(table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False))
        .where(
            and_(
                table1.c["status"].in_(
                    [sql_constant(AttemptStatus.RUNNING.value), sql_constant(AttemptStatus.STALE.value)]
                ),
                table2.c["lifecycle"].in_(
                    [sql_constant(TaskLifecycle.QUEUED.value), sql_constant(TaskLifecycle.RUNNING.value)]
                ),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {})


def find_for_attempt_evidence(conn):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    query3 = (
        select(
            table2.c["route_id"],
            table2.c["lifecycle"],
            table1.c["attempt_id"],
            table1.c["status"],
            table1.c["worktree_path"],
            table1.c["branch_name"],
            table1.c["metadata_json"],
        )
        .select_from(table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False))
        .order_by(table1.c["started_at"].desc(), table1.c["attempt_id"].desc())
        .limit(sql_constant(1))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {})


def find_metadata_json_by_attempt_id(conn, attempt_id):
    query1 = (
        select(tables["attempts"].c["metadata_json"])
        .select_from(tables["attempts"])
        .where((tables["attempts"].c["attempt_id"] == bindparam("p0")))
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id})


def record_result_db(conn, attempt_id, task_id):
    query1 = (
        select(tables["attempts"].c["metadata_json"], tables["attempts"].c["status"])
        .select_from(tables["attempts"])
        .where(
            and_(
                (tables["attempts"].c["attempt_id"] == bindparam("p0")),
                (tables["attempts"].c["task_id"] == bindparam("p1")),
            )
        )
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id, "p1": task_id})


def record_result_db_2(conn, attempt_id, task_id):
    query1 = (
        select(
            func.json_extract(
                tables["attempts"].c["metadata_json"],
                sql_constant("$.result_validation"),
            )
        )
        .select_from(tables["attempts"])
        .where(
            and_(
                (tables["attempts"].c["attempt_id"] == bindparam("p0")),
                (tables["attempts"].c["task_id"] == bindparam("p1")),
            )
        )
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id, "p1": task_id})


def record_result_db_3(conn, attempt_id, task_id):
    query1 = (
        select(tables["attempts"].c["status"])
        .select_from(tables["attempts"])
        .where(
            and_(
                (tables["attempts"].c["attempt_id"] == bindparam("p0")),
                (tables["attempts"].c["task_id"] == bindparam("p1")),
            )
        )
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id, "p1": task_id})


def record_diagnostic(conn, value_1, value_2):
    query1 = (
        select(
            func.json_extract(
                tables["attempts"].c["metadata_json"],
                sql_constant("$.submission_validation_counts"),
            )
        )
        .select_from(tables["attempts"])
        .where(
            and_(
                (tables["attempts"].c["attempt_id"] == bindparam("p0")),
                (tables["attempts"].c["task_id"] == bindparam("p1")),
            )
        )
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": value_1, "p1": value_2})


def find_for_prepared_attempt_dispatch_priority(conn, attempt_id):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    query3 = (
        select(table2.c["route_id"], table2.c["workstream_id"])
        .select_from(table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False))
        .where((table1.c["attempt_id"] == bindparam("p0")))
        .limit(sql_constant(1))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": attempt_id})


def find_status_and_metadata_json_by_attempt_id(conn, attempt_id):
    query1 = (
        select(tables["attempts"].c["status"], tables["attempts"].c["metadata_json"])
        .select_from(tables["attempts"])
        .where((tables["attempts"].c["attempt_id"] == bindparam("p0")))
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id})


def find_for_completed_by_worker_result(conn, attempt_id):
    table1 = tables["attempts"].alias("a")
    table2 = tables["worker_results"].alias("wr")
    query3 = (
        select(sql_constant(1))
        .select_from(
            table1.join(
                table2,
                (table2.c["attempt_id"] == table1.c["attempt_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                (table1.c["attempt_id"] == bindparam("p0")),
                (table1.c["status"] == sql_constant(AttemptStatus.COMPLETED.value)),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": attempt_id})


def find_for_current_dispatch_target(conn, attempt_id):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    table3 = tables["artifacts"].alias("ar")
    query4 = (
        select(
            table1.c["task_id"],
            table2.c["workstream_id"],
            table2.c["route_id"],
            table1.c["worktree_path"],
            table3.c["path"],
            table2.c["routing_mode"],
            table2.c["requested_worker"],
        )
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False).join(
                table3, (table3.c["task_id"] == table2.c["task_id"]), isouter=False
            )
        )
        .where(
            and_(
                table1.c["attempt_id"] == bindparam("p0"),
                table1.c["status"] == sql_constant(AttemptStatus.PREPARED.value),
                table2.c["lifecycle"].in_(
                    [sql_constant(TaskLifecycle.QUEUED.value), sql_constant(TaskLifecycle.RUNNING.value)]
                ),
                table3.c["artifact_type"] == sql_constant("prompt"),
                or_(
                    table3.c["attempt_id"] == table1.c["attempt_id"],
                    table3.c["attempt_id"].is_(null()),
                ),
                or_(
                    func.json_extract(
                        table1.c["metadata_json"],
                        sql_constant("$.resume.not_before_at"),
                    ).is_(null()),
                    func.julianday(
                        func.json_extract(
                            table1.c["metadata_json"],
                            sql_constant("$.resume.not_before_at"),
                        )
                    )
                    <= func.julianday(sql_constant("now")),
                ),
            )
        )
        .order_by(table3.c["created_at"].desc(), table3.c["artifact_id"].desc())
        .limit(sql_constant(1))
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": attempt_id})


def find_for_prepared_attempt_event_and_route(conn, attempt_id):
    table1 = tables["attempts"].alias("a")
    table2 = tables["task_events"].alias("te")
    table3 = tables["github_events"].alias("ge")
    table4 = tables["route_decisions"].alias("rd")
    query5 = (
        select(table3.c["payload_json"], table4.c["route_id"])
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False)
            .join(table3, (table3.c["event_id"] == table2.c["event_id"]), isouter=False)
            .join(table4, (table4.c["task_id"] == table1.c["task_id"]), isouter=False)
        )
        .where((table1.c["attempt_id"] == bindparam("p0")))
        .order_by(
            case(
                (sql_constant("trigger"), sql_constant(0)),
                value=table2.c["relationship"],
                else_=sql_constant(1),
            ).asc(),
            table2.c["created_at"].asc(),
            table4.c["created_at"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(table1, table2, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query5, {"p0": attempt_id})


def find_for_prepared_attempt_event_and_route_2(conn, attempt_id):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    table3 = tables["work_items"].alias("wi")
    table4 = tables["route_decisions"].alias("rd")
    query5 = (
        select(
            table3.c["work_item_id"],
            table3.c["title"],
            table3.c["description"],
            table1.c["branch_name"],
            table4.c["route_id"],
            table2.c["metadata_json"],
        )
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False)
            .join(
                table3,
                (table3.c["workstream_id"] == table2.c["workstream_id"]),
                isouter=False,
            )
            .join(table4, (table4.c["task_id"] == table2.c["task_id"]), isouter=False)
        )
        .where(
            and_(
                (table1.c["attempt_id"] == bindparam("p0")),
                (table3.c["origin_type"] == sql_constant("web")),
            )
        )
        .order_by(table4.c["created_at"].desc())
        .limit(sql_constant(1))
        .correlate_except(table1, table2, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query5, {"p0": attempt_id})


def find_for_model_for_dispatch(conn, workstream_id, attempt_id):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    query3 = (
        select(table1.c["failure_json"])
        .select_from(table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False))
        .where(
            and_(
                table2.c["workstream_id"] == bindparam("p0"),
                table1.c["attempt_id"] != bindparam("p1"),
                table1.c["status"].in_(
                    [
                        sql_constant(AttemptStatus.COMPLETED.value),
                        sql_constant(AttemptStatus.FAILED.value),
                        sql_constant(AttemptStatus.CANCELED.value),
                    ]
                ),
            )
        )
        .order_by(
            func.coalesce(table1.c["finished_at"], table1.c["started_at"]).desc(),
            table1.c["attempt_id"].desc(),
        )
        .limit(sql_constant(2))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": workstream_id, "p1": attempt_id})


def find_task_id_and_metadata_json_by_attempt_id(conn, attempt_id):
    query1 = (
        select(tables["attempts"].c["task_id"], tables["attempts"].c["metadata_json"])
        .select_from(tables["attempts"])
        .where((tables["attempts"].c["attempt_id"] == bindparam("p0")))
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id})


def find_for_supervision_observation_current(conn, attempt_id, value_2):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    table4 = tables["attempts"].alias("a2")
    query5 = (
        select(func.max(table4.c["attempt_no"]))
        .select_from(table4)
        .where((table4.c["task_id"] == table1.c["task_id"]))
        .correlate_except(table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query3 = (
        select(table1.c["status"], table1.c["heartbeat_at"], table1.c["metadata_json"])
        .select_from(table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False))
        .where(
            and_(
                table1.c["attempt_id"] == bindparam("p0"),
                or_(
                    table2.c["lifecycle"].in_(
                        [sql_constant(TaskLifecycle.QUEUED.value), sql_constant(TaskLifecycle.RUNNING.value)]
                    ),
                    and_(
                        bindparam("p1") == sql_constant("failed"),
                        table2.c["lifecycle"] == sql_constant(TaskLifecycle.FAILED.value),
                    ),
                ),
                table1.c["attempt_no"] == query5.scalar_subquery(),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": attempt_id, "p1": value_2})


def find_for_recover_failed_timeout_attempts(conn, repo_id, run_now):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    table3 = tables["workstreams"].alias("w")
    table5 = tables["attempts"].alias("a2")
    query6 = (
        select(func.max(table5.c["attempt_no"]))
        .select_from(table5)
        .where((table5.c["task_id"] == table1.c["task_id"]))
        .correlate_except(table5)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query4 = (
        select(
            table1.c["attempt_id"],
            table1.c["task_id"],
            table2.c["workstream_id"],
            table1.c["worktree_path"],
            table1.c["failure_json"],
            table1.c["metadata_json"],
            table1.c["heartbeat_at"],
        )
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False).join(
                table3,
                (table3.c["workstream_id"] == table2.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table3.c["repo_id"] == bindparam("p0"),
                table1.c["status"] == sql_constant(AttemptStatus.FAILED.value),
                table2.c["lifecycle"] == sql_constant(TaskLifecycle.FAILED.value),
                table3.c["lifecycle"] == sql_constant(WorkstreamLifecycle.FAILED.value),
                or_(
                    table1.c["finished_at"].is_(null()),
                    table1.c["finished_at"] < bindparam("p1"),
                ),
                table1.c["attempt_no"] == query6.scalar_subquery(),
            )
        )
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": repo_id, "p1": run_now})


def find_worktree_path_by_attempt_id(conn, attempt_id):
    query1 = (
        select(tables["attempts"].c["worktree_path"])
        .select_from(tables["attempts"])
        .where((tables["attempts"].c["attempt_id"] == bindparam("p0")))
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id})


def find_status_by_attempt_id(conn, attempt_id):
    query1 = (
        select(tables["attempts"].c["status"])
        .select_from(tables["attempts"])
        .where((tables["attempts"].c["attempt_id"] == bindparam("p0")))
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id})


def find_for_run_command_with_heartbeat(conn, attempt_id, task_id):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["attempts"])
        .where(
            and_(
                (tables["attempts"].c["attempt_id"] == bindparam("p0")),
                (tables["attempts"].c["task_id"] == bindparam("p1")),
            )
        )
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id, "p1": task_id})


def find_task_id_and_status_and_metadata_json_by_attempt_id(conn, attempt_id):
    query1 = (
        select(
            tables["attempts"].c["task_id"],
            tables["attempts"].c["status"],
            tables["attempts"].c["metadata_json"],
        )
        .select_from(tables["attempts"])
        .where((tables["attempts"].c["attempt_id"] == bindparam("p0")))
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id})


def find_for_bound_attempt_for_subject(conn, task_id):
    query1 = (
        select(tables["attempts"].c["attempt_id"])
        .select_from(tables["attempts"])
        .where(
            and_(
                (tables["attempts"].c["task_id"] == bindparam("p0")),
                tables["attempts"]
                .c["status"]
                .in_(
                    [
                        sql_constant(AttemptStatus.PREPARED.value),
                        sql_constant(AttemptStatus.RUNNING.value),
                        sql_constant(AttemptStatus.STALE.value),
                    ]
                ),
            )
        )
        .order_by(
            tables["attempts"].c["attempt_no"].desc(),
            tables["attempts"].c["attempt_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_task_id_by_attempt_id(conn, attempt_id):
    query1 = (
        select(tables["attempts"].c["task_id"])
        .select_from(tables["attempts"])
        .where((tables["attempts"].c["attempt_id"] == bindparam("p0")))
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id})


@cache
def _find_for_latest_attempt_statement():
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    query3 = (
        select(
            table1.c["attempt_id"],
            table1.c["status"],
            table1.c["branch_name"],
            table1.c["worktree_path"],
            table1.c["started_at"],
            table1.c["finished_at"],
        )
        .select_from(table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False))
        .where((table2.c["task_id"] == bindparam("p0")))
        .order_by(table1.c["attempt_no"].desc())
        .limit(sql_constant(1))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return query3


def find_for_latest_attempt(conn, task_id):
    return statements.read(conn, _find_for_latest_attempt_statement(), {"p0": task_id})


def list_board(conn):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    table3 = tables["workstreams"].alias("w")
    query4 = (
        select(table3.c["repo_id"], func.count(literal_column("*")))
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False).join(
                table3,
                (table3.c["workstream_id"] == table2.c["workstream_id"]),
                isouter=False,
            )
        )
        .where((table1.c["status"] == sql_constant(AttemptStatus.RUNNING.value)))
        .group_by(table3.c["repo_id"])
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {})


def find_for_project_item(conn, task_id):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["attempts"])
        .where(
            and_(
                (tables["attempts"].c["task_id"] == bindparam("p0")),
                (tables["attempts"].c["status"] == sql_constant(AttemptStatus.PREPARED.value)),
            )
        )
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_record_by_task_id(conn, task_id):
    query1 = (
        select(tables["attempts"])
        .select_from(tables["attempts"])
        .where((tables["attempts"].c["task_id"] == bindparam("p0")))
        .order_by(tables["attempts"].c["attempt_no"].asc())
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_project_item_2(conn, task_id):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["attempts"])
        .where(
            and_(
                (tables["attempts"].c["task_id"] == bindparam("p0")),
                not_(
                    func.json_extract(
                        tables["attempts"].c["metadata_json"],
                        sql_constant("$.process_stopped_at"),
                    ).is_(null())
                ),
            )
        )
        .order_by(tables["attempts"].c["attempt_no"].desc())
        .limit(sql_constant(1))
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


@cache
def _find_record_by_task_id_2_statement():
    query1 = (
        select(tables["attempts"])
        .select_from(tables["attempts"])
        .where((tables["attempts"].c["task_id"] == bindparam("p0")))
        .order_by(tables["attempts"].c["attempt_no"].desc())
        .limit(sql_constant(1))
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return query1


def find_record_by_task_id_2(conn, task_id):
    return statements.read(conn, _find_record_by_task_id_2_statement(), {"p0": task_id})


def find_for_latest_attempt_for_task(conn, task_id):
    query1 = (
        select(
            tables["attempts"].c["attempt_id"],
            tables["attempts"].c["attempt_no"],
            tables["attempts"].c["status"],
            tables["attempts"].c["worktree_path"],
            tables["attempts"].c["branch_name"],
            tables["attempts"].c["started_at"],
            tables["attempts"].c["heartbeat_at"],
            tables["attempts"].c["finished_at"],
            tables["attempts"].c["failure_json"],
            tables["attempts"].c["metadata_json"],
        )
        .select_from(tables["attempts"])
        .where((tables["attempts"].c["task_id"] == bindparam("p0")))
        .order_by(
            tables["attempts"].c["attempt_no"].desc(),
            tables["attempts"].c["started_at"].desc(),
            tables["attempts"].c["attempt_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_build_attempt(conn, attempt_id):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    query3 = (
        select(
            table1.c["attempt_id"],
            table1.c["task_id"],
            table1.c["attempt_no"],
            table1.c["status"],
            table1.c["worktree_path"],
            table1.c["branch_name"],
            table1.c["started_at"],
            table1.c["heartbeat_at"],
            table1.c["finished_at"],
            table1.c["failure_json"],
            table1.c["metadata_json"],
            table2.c["workstream_id"],
            table2.c["lifecycle"].label("task_lifecycle"),
        )
        .select_from(table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False))
        .where((table1.c["attempt_id"] == bindparam("p0")))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": attempt_id})


def find_for_attempt_facts(conn, task_id):
    table1 = tables["attempts"].alias("a")
    table2 = tables["worker_results"].alias("wr")
    query3 = (
        select(
            table1.c["attempt_id"],
            table1.c["status"].label("attempt_status"),
            table1.c["started_at"],
            table1.c["heartbeat_at"],
            table1.c["finished_at"],
            table1.c["worktree_path"],
            func.json_extract(table1.c["failure_json"], sql_constant("$.status")).label("attempt_failure"),
            func.json_extract(table1.c["metadata_json"], sql_constant("$.worker_started_at")).label(
                "worker_started_at"
            ),
            func.json_extract(table1.c["metadata_json"], sql_constant("$.dispatch")).label("dispatch"),
            func.json_extract(table1.c["metadata_json"], sql_constant("$.process_stopped_at")).label(
                "process_stopped_at"
            ),
            func.json_extract(table1.c["metadata_json"], sql_constant("$.resume.not_before_at")).label(
                "dispatch_not_before"
            ),
            table2.c["result_id"],
            table2.c["output_type"],
            table2.c["created_at"].label("result_created_at"),
            func.json_extract(table2.c["metadata_json"], sql_constant("$.audit.status")).label("audit_status"),
            func.json_extract(table2.c["metadata_json"], sql_constant("$.audit.audited_at")).label("audited_at"),
            func.json_extract(table2.c["metadata_json"], sql_constant("$.audit.reason_code")).label("audit_reason"),
            func.json_extract(table2.c["metadata_json"], sql_constant("$.audit_observation.retryable")).label(
                "audit_retryable"
            ),
            func.json_extract(table2.c["metadata_json"], sql_constant("$.audit.action_scope")).label("audited_scope"),
            func.json_extract(
                table2.c["metadata_json"],
                sql_constant("$.normalized_payload.planned_github_actions"),
            ).label("planned_actions"),
            func.json_extract(
                table2.c["metadata_json"],
                sql_constant("$.audit_observation.next_retry_at"),
            ).label("audit_retry_at"),
        )
        .select_from(
            table1.join(
                table2,
                and_(
                    (table2.c["task_id"] == table1.c["task_id"]),
                    (table2.c["attempt_id"] == table1.c["attempt_id"]),
                ),
                isouter=True,
            )
        )
        .where((table1.c["task_id"] == bindparam("p0")))
        .order_by(table1.c["attempt_no"].desc())
        .limit(sql_constant(1))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": task_id})


def find_for_is_current(conn, scope):
    table1 = tables["attempts"].alias("a")
    table2 = tables["worker_results"].alias("wr")
    query3 = (
        select(
            table1.c["attempt_id"],
            table1.c["status"],
            table2.c["result_id"],
            func.json_extract(table2.c["metadata_json"], sql_constant("$.audit.status")),
        )
        .select_from(
            table1.join(
                table2,
                and_(
                    (table2.c["task_id"] == table1.c["task_id"]),
                    (table2.c["attempt_id"] == table1.c["attempt_id"]),
                ),
                isouter=True,
            )
        )
        .where((table1.c["task_id"] == bindparam("p0")))
        .order_by(table1.c["attempt_no"].desc())
        .limit(sql_constant(1))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": scope})


def find_started_at_by_task_id(conn, target_id):
    query1 = (
        select(tables["attempts"].c["started_at"])
        .select_from(tables["attempts"])
        .where((tables["attempts"].c["task_id"] == bindparam("p0")))
        .order_by(tables["attempts"].c["attempt_no"].asc())
        .limit(sql_constant(1))
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": target_id})


def find_for_task_context(conn, task_id):
    table1 = tables["attempts"].alias("a")
    table2 = tables["worker_results"].alias("wr")
    query3 = (
        select(
            table1.c["attempt_id"],
            table1.c["status"],
            table1.c["branch_name"],
            table1.c["started_at"],
            table1.c["finished_at"],
            func.json_extract(table1.c["metadata_json"], sql_constant("$.existing_pr_head_sha")).label("input_sha"),
            table2.c["result_id"],
            table2.c["output_type"],
            func.json_extract(table2.c["metadata_json"], sql_constant("$.audit.status")).label("audit_status"),
            func.json_extract(
                table2.c["metadata_json"],
                sql_constant("$.audit.action_scope.result_head_sha"),
            ).label("result_sha"),
            case(
                (
                    (func.length(table2.c["verification_json"]) <= sql_constant(8000)),
                    table2.c["verification_json"],
                )
            ).label("reported_verification"),
        )
        .select_from(
            table1.join(
                table2,
                and_(
                    (table2.c["attempt_id"] == table1.c["attempt_id"]),
                    (table2.c["task_id"] == table1.c["task_id"]),
                ),
                isouter=True,
            )
        )
        .where((table1.c["task_id"] == bindparam("p0")))
        .order_by(table1.c["attempt_no"].desc())
        .limit(sql_constant(1))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": task_id})


def find_for_retrigger_failed_github_task(conn, task_id):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["attempts"])
        .where(
            and_(
                (tables["attempts"].c["task_id"] == bindparam("p0")),
                tables["attempts"]
                .c["status"]
                .in_(
                    [
                        sql_constant(AttemptStatus.PREPARED.value),
                        sql_constant(AttemptStatus.RUNNING.value),
                        sql_constant(AttemptStatus.STALE.value),
                    ]
                ),
            )
        )
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_metadata_json_by_task_id(conn, task_id):
    query1 = (
        select(tables["attempts"].c["metadata_json"])
        .select_from(tables["attempts"])
        .where((tables["attempts"].c["task_id"] == bindparam("p0")))
        .order_by(tables["attempts"].c["attempt_no"].desc())
        .limit(sql_constant(1))
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_attempt_no_and_worktree_path_and_branch_name_and_metadata_json_by_attempt_id(conn, previous_attempt_id):
    query1 = (
        select(
            tables["attempts"].c["attempt_no"],
            tables["attempts"].c["worktree_path"],
            tables["attempts"].c["branch_name"],
            tables["attempts"].c["metadata_json"],
        )
        .select_from(tables["attempts"])
        .where((tables["attempts"].c["attempt_id"] == bindparam("p0")))
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": previous_attempt_id})


def find_for_create_resume_attempt_and_prompt(conn, task_id):
    query1 = (
        select(func.coalesce(func.max(tables["attempts"].c["attempt_no"]), sql_constant(0)))
        .select_from(tables["attempts"])
        .where((tables["attempts"].c["task_id"] == bindparam("p0")))
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_run(conn, task_id, attempt_id):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    table3 = tables["workstreams"].alias("w")
    query4 = (
        select(
            table3.c["repo_id"],
            table1.c["worktree_path"],
            table1.c["branch_name"],
            table1.c["metadata_json"],
        )
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False).join(
                table3,
                (table3.c["workstream_id"] == table2.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                (table1.c["task_id"] == bindparam("p0")),
                (table1.c["attempt_id"] == bindparam("p1")),
            )
        )
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": task_id, "p1": attempt_id})


def find_for_episode_usage(conn, episode_id):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    query3 = (
        select(table1.c["metadata_json"])
        .select_from(table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False))
        .where(
            (func.json_extract(table2.c["metadata_json"], sql_constant("$.remediation_episode_id")) == bindparam("p0"))
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": episode_id})


def find_for_prepare(conn):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    query3 = (
        select(table1.c["task_id"])
        .distinct()
        .select_from(table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False))
        .where(
            and_(
                (
                    func.json_extract(
                        table2.c["metadata_json"],
                        sql_constant("$.execution_attention.reason_code"),
                    )
                    == sql_constant("result_contract_cutover")
                ),
                (occupied_attempt(table1)),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {})


def find_for_prepare_2(conn, task_id):
    table1 = tables["attempts"].alias("a")
    query2 = (
        select(table1)
        .select_from(table1)
        .where(
            and_(
                (table1.c["task_id"] == bindparam("p0")),
                (occupied_attempt(table1)),
            )
        )
        .correlate_except(table1)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query2, {"p0": task_id})


def find_for_supervise_running_attempts(conn, repo_id, task_id, task_id_3):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    table3 = tables["workstreams"].alias("w")
    query4 = (
        select(
            table1.c["attempt_id"],
            table1.c["task_id"],
            table2.c["workstream_id"],
            table1.c["heartbeat_at"],
            table1.c["started_at"],
            table1.c["status"],
            table1.c["metadata_json"],
        )
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False).join(
                table3,
                (table3.c["workstream_id"] == table2.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table3.c["repo_id"] == bindparam("p0"),
                or_(
                    occupied_attempt(table1),
                    and_(
                        table1.c["status"] == sql_constant(AttemptStatus.PREPARED.value),
                        not_(
                            func.json_extract(
                                table1.c["metadata_json"],
                                sql_constant("$.result_validation"),
                            ).is_(null())
                        ),
                    ),
                ),
                table2.c["lifecycle"].in_(
                    [sql_constant(TaskLifecycle.QUEUED.value), sql_constant(TaskLifecycle.RUNNING.value)]
                ),
                runnable_task(table2),
                or_(bindparam("p1").is_(null()), table2.c["task_id"] == bindparam("p2")),
            )
        )
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": repo_id, "p1": task_id, "p2": task_id_3})


def find_for_running_attempt_count(conn, repo_id):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    table3 = tables["workstreams"].alias("w")
    query4 = (
        select(func.count(literal_column("*")))
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False).join(
                table3,
                (table3.c["workstream_id"] == table2.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table3.c["repo_id"] == bindparam("p0"),
                occupied_attempt(table1),
                table2.c["lifecycle"].in_(
                    [sql_constant(TaskLifecycle.QUEUED.value), sql_constant(TaskLifecycle.RUNNING.value)]
                ),
            )
        )
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": repo_id})


def find_for_target(conn, task_id):
    query1 = (
        select(
            tables["attempts"].c["attempt_id"],
            tables["attempts"].c["status"],
            func.json_extract(tables["attempts"].c["metadata_json"], sql_constant("$.dispatch.pid")).label("pid"),
            func.json_extract(
                tables["attempts"].c["metadata_json"],
                sql_constant("$.process_stopped_at"),
            ).label("stopped_at"),
        )
        .select_from(tables["attempts"])
        .where((tables["attempts"].c["task_id"] == bindparam("p0")))
        .order_by(tables["attempts"].c["attempt_no"].desc())
        .limit(sql_constant(1))
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_latest_workstream_update_branch(conn, workstream_id, exclude_task_id=None):
    attempt, task = tables["attempts"], tables["tasks"]
    stmt = (
        select(attempt.c.branch_name)
        .join(task, task.c.task_id == attempt.c.task_id)
        .where(
            task.c.workstream_id == workstream_id,
            task.c.route_id == sql_constant("update-existing-pr"),
            attempt.c.branch_name.is_not(None),
            attempt.c.branch_name != sql_constant(""),
        )
    )
    if exclude_task_id:
        stmt = stmt.where(task.c.task_id != exclude_task_id)
    stmt = stmt.order_by(
        func.coalesce(
            attempt.c.finished_at,
            attempt.c.started_at,
            attempt.c.heartbeat_at,
            task.c.updated_at,
            task.c.created_at,
        ).desc(),
        attempt.c.attempt_no.desc(),
        attempt.c.attempt_id.desc(),
    ).limit(1)
    return statements.read(conn, stmt)


def record_remote_terminal_state_locked(conn, task_id):
    query1 = (
        select(
            tables["attempts"].c["attempt_id"],
            tables["attempts"].c["status"],
            tables["attempts"].c["metadata_json"],
        )
        .select_from(tables["attempts"])
        .where(
            and_(
                (tables["attempts"].c["task_id"] == bindparam("p0")),
                (
                    or_(
                        (tables["attempts"].c["status"] == sql_constant(AttemptStatus.PREPARED.value)),
                        (occupied_attempt(tables["attempts"])),
                    )
                ),
            )
        )
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


@lru_cache(maxsize=2)
def _supervision_transition_due_filtered_statement(scoped):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    table3 = tables["workstreams"].alias("w")
    table4 = tables["worker_results"].alias("wr")
    query5 = (
        select(sql_constant(1))
        .select_from(table4)
        .where((table4.c["attempt_id"] == table1.c["attempt_id"]))
        .correlate_except(table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query6 = (
        select(
            table1.c["attempt_id"],
            table1.c["task_id"],
            table1.c["status"],
            table1.c["heartbeat_at"],
            table1.c["started_at"],
            table1.c["metadata_json"],
            query5.exists(),
        )
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False).join(
                table3,
                (table3.c["workstream_id"] == table2.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                or_(
                    occupied_attempt(table1),
                    and_(
                        table1.c["status"] == sql_constant(AttemptStatus.PREPARED.value),
                        not_(
                            func.json_extract(
                                table1.c["metadata_json"],
                                sql_constant("$.result_validation"),
                            ).is_(null())
                        ),
                    ),
                ),
                table2.c["lifecycle"].in_(
                    [sql_constant(TaskLifecycle.QUEUED.value), sql_constant(TaskLifecycle.RUNNING.value)]
                ),
                runnable_task(table2),
            )
        )
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    if scoped:
        query6 = query6.where(table3.c.repo_id.in_(bindparam("items", expanding=True)))
    return query6


def supervision_transition_due_filtered(conn, items):
    return statements.read(conn, _supervision_transition_due_filtered_statement(bool(items)), {"items": items})


@lru_cache(maxsize=2)
def _has_local_work_filtered_statement(scoped):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    table3 = tables["workstreams"].alias("w")
    table4 = tables["execution_subjects"].alias("c")
    table8 = tables["attempts"].alias("latest")
    query9 = (
        select(func.max(table8.c["attempt_no"]))
        .select_from(table8)
        .where((table8.c["task_id"] == table2.c["task_id"]))
        .correlate_except(table8)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query5 = (
        select(sql_constant(1))
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False)
            .join(
                table3,
                (table3.c["workstream_id"] == table2.c["workstream_id"]),
                isouter=False,
            )
            .join(
                table4,
                or_(
                    (
                        and_(
                            (table4.c["subject_type"] == sql_constant("task")),
                            (table4.c["subject_id"] == table2.c["task_id"]),
                        )
                    ),
                    (
                        and_(
                            (table4.c["subject_type"] == sql_constant("remediation_episode")),
                            (
                                table4.c["subject_id"]
                                == func.json_extract(
                                    table2.c["metadata_json"],
                                    sql_constant("$.remediation_episode_id"),
                                )
                            ),
                        )
                    ),
                ),
                isouter=False,
            )
        )
        .where(
            and_(
                table1.c["status"] == sql_constant(AttemptStatus.FAILED.value),
                table2.c["lifecycle"] == sql_constant(TaskLifecycle.FAILED.value),
                table3.c["lifecycle"] == sql_constant(WorkstreamLifecycle.FAILED.value),
                func.json_extract(table1.c["failure_json"], sql_constant("$.status")) == sql_constant("failed_timeout"),
                or_(
                    func.json_extract(
                        table1.c["metadata_json"],
                        sql_constant("$.inspection_retry.next_retry_at"),
                    ).is_(null()),
                    func.julianday(
                        func.json_extract(
                            table1.c["metadata_json"],
                            sql_constant("$.inspection_retry.next_retry_at"),
                        )
                    )
                    <= func.julianday(bindparam("p0")),
                ),
                table4.c["binding_status"] == sql_constant(BindingStatus.ACTIVE.value),
                runnable_task(table2),
                or_(
                    table1.c["finished_at"].is_(null()),
                    func.julianday(table1.c["finished_at"]) <= func.julianday(bindparam("p1")),
                ),
                table1.c["attempt_no"] == query9.scalar_subquery(),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(table1, table2, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    if scoped:
        query5 = query5.where(table3.c.repo_id.in_(bindparam("items", expanding=True)))
    return query5


def has_local_work_filtered(conn, items, value_1, value_2):
    return statements.read(
        conn, _has_local_work_filtered_statement(bool(items)), {"p0": value_1, "p1": value_2, "items": items}
    )


@lru_cache(maxsize=2)
def _has_local_work_filtered_2_statement(scoped):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    table3 = tables["workstreams"].alias("w")
    table4 = tables["repos"].alias("r")
    query5 = (
        select(table3.c["repo_id"], table4.c["full_name"])
        .distinct()
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False)
            .join(
                table3,
                (table3.c["workstream_id"] == table2.c["workstream_id"]),
                isouter=False,
            )
            .join(table4, (table4.c["repo_id"] == table3.c["repo_id"]), isouter=False)
        )
        .where(
            and_(
                table1.c["status"] == sql_constant(AttemptStatus.PREPARED.value),
                func.json_extract(table2.c["metadata_json"], sql_constant("$.dispatch_wait")).is_(null()),
                or_(
                    func.json_extract(
                        table1.c["metadata_json"],
                        sql_constant("$.resume.not_before_at"),
                    ).is_(null()),
                    func.julianday(
                        func.json_extract(
                            table1.c["metadata_json"],
                            sql_constant("$.resume.not_before_at"),
                        )
                    )
                    <= func.julianday(bindparam("p0")),
                ),
                table2.c["lifecycle"].in_(
                    [sql_constant(TaskLifecycle.QUEUED.value), sql_constant(TaskLifecycle.RUNNING.value)]
                ),
                runnable_task(table2),
            )
        )
        .correlate_except(table1, table2, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    if scoped:
        query5 = query5.where(table3.c.repo_id.in_(bindparam("items", expanding=True)))
    return query5


def has_local_work_filtered_2(conn, items, value_1):
    return statements.read(conn, _has_local_work_filtered_2_statement(bool(items)), {"p0": value_1, "items": items})


def has_occupied_runnable_attempt_filtered(conn, items):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    table3 = tables["workstreams"].alias("w")
    query4 = (
        select(sql_constant(1))
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False).join(
                table3,
                (table3.c["workstream_id"] == table2.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table3.c["repo_id"].in_(bindparam("items", expanding=True)),
                table2.c["lifecycle"].in_(
                    [sql_constant(TaskLifecycle.QUEUED.value), sql_constant(TaskLifecycle.RUNNING.value)]
                ),
                occupied_attempt(table1),
                runnable_task(table2),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"items": items})


def running_attempt_count_for_repos_filtered(conn, items):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    table3 = tables["workstreams"].alias("w")
    query4 = (
        select(func.count(literal_column("*")))
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False).join(
                table3,
                (table3.c["workstream_id"] == table2.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table3.c["repo_id"].in_(bindparam("items", expanding=True)),
                occupied_attempt(table1),
                table2.c["lifecycle"].in_(
                    [sql_constant(TaskLifecycle.QUEUED.value), sql_constant(TaskLifecycle.RUNNING.value)]
                ),
            )
        )
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"items": items})


@cache
def _lifecycle_facts_filtered_statement():
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    table3 = tables["workstreams"].alias("w")
    table4 = tables["github_sources"].alias("gs")
    query5 = (
        select(
            table1.c["attempt_id"],
            table1.c["task_id"],
            table1.c["status"],
            table1.c["metadata_json"],
            table4.c["state"].label("source_state"),
        )
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False)
            .join(
                table3,
                (table3.c["workstream_id"] == table2.c["workstream_id"]),
                isouter=False,
            )
            .join(
                table4,
                (table4.c["source_id"] == table3.c["primary_source_id"]),
                isouter=True,
            )
        )
        .where(
            and_(
                table2.c["workstream_id"].in_(bindparam("items", expanding=True)),
                (occupied_attempt(table1)),
            )
        )
        .correlate_except(table1, table2, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return query5


def lifecycle_facts_filtered(conn, items):
    return statements.read(conn, _lifecycle_facts_filtered_statement(), {"items": items})


def find_for_queue_prepared_attempts(conn, repo_id, capacity):
    table1 = tables["attempts"].alias("a")
    table2 = tables["tasks"].alias("t")
    table3 = tables["workstreams"].alias("w")
    table4 = tables["artifacts"].alias("ar")
    table5 = tables["artifacts"].alias("ar2")
    query6 = (
        select(table5.c["artifact_id"])
        .select_from(table5)
        .where(
            and_(
                table5.c["task_id"] == table2.c["task_id"],
                table5.c["artifact_type"] == sql_constant("prompt"),
                or_(
                    table5.c["attempt_id"] == table1.c["attempt_id"],
                    table5.c["attempt_id"].is_(null()),
                ),
            )
        )
        .order_by(table5.c["created_at"].desc(), table5.c["artifact_id"].desc())
        .limit(sql_constant(1))
        .correlate_except(table5)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query7 = (
        select(
            table1.c["attempt_id"],
            table1.c["task_id"],
            table2.c["workstream_id"],
            table2.c["route_id"],
            table1.c["worktree_path"],
            table4.c["path"],
            table2.c["routing_mode"],
            table2.c["requested_worker"],
        )
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False)
            .join(
                table3,
                (table3.c["workstream_id"] == table2.c["workstream_id"]),
                isouter=False,
            )
            .join(
                table4,
                and_(
                    (table4.c["task_id"] == table2.c["task_id"]),
                    (table4.c["artifact_id"] == query6.scalar_subquery()),
                ),
                isouter=False,
            )
        )
        .where(
            and_(
                table3.c["repo_id"] == bindparam("p0"),
                table1.c["status"] == sql_constant(AttemptStatus.PREPARED.value),
                func.json_extract(table2.c["metadata_json"], sql_constant("$.dispatch_wait")).is_(null()),
                runnable_task(table2),
                table2.c["lifecycle"].in_(
                    [sql_constant(TaskLifecycle.QUEUED.value), sql_constant(TaskLifecycle.RUNNING.value)]
                ),
                table4.c["artifact_type"] == sql_constant("prompt"),
                or_(
                    func.json_extract(
                        table1.c["metadata_json"],
                        sql_constant("$.resume.not_before_at"),
                    ).is_(null()),
                    func.julianday(
                        func.json_extract(
                            table1.c["metadata_json"],
                            sql_constant("$.resume.not_before_at"),
                        )
                    )
                    <= func.julianday(sql_constant("now")),
                ),
            )
        )
        .order_by(
            (
                (
                    case(
                        (sql_constant("P0"), sql_constant(0)),
                        (sql_constant("P1"), sql_constant(1)),
                        (sql_constant("P2"), sql_constant(2)),
                        value=table2.c["priority"],
                        else_=sql_constant(3),
                    )
                )
                - func.min(
                    sql_constant(3),
                    cast(
                        func.max(
                            sql_constant(0),
                            (func.julianday(sql_constant("now")) - func.julianday(table2.c["created_at"])),
                        ),
                        Integer,
                    ),
                )
            ).asc(),
            case(
                (
                    (table2.c["route_id"] == sql_constant("update-existing-pr")),
                    sql_constant(0),
                ),
                (
                    table2.c["workstream_id"].like(sql_constant("github:%!%")),
                    sql_constant(1),
                ),
                else_=sql_constant(2),
            ).asc(),
            table1.c["started_at"].asc(),
            table1.c["attempt_id"].asc(),
        )
        .limit(bindparam("p1"))
        .correlate_except(table1, table2, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query7, {"p0": repo_id, "p1": capacity})
