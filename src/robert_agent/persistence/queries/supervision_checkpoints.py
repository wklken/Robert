"""Shared queries operations for supervision_checkpoints."""

from robert_agent.foundation.contracts.delivery import OutputType
from robert_agent.foundation.contracts.execution import AttemptStatus
from robert_agent.foundation.contracts.work import TaskLifecycle

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import (
    select,
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


def find_for_checkpoints(conn, scope_type, scope_id):
    query1 = (
        select(
            tables["supervision_checkpoints"].c["rule_id"],
            tables["supervision_checkpoints"].c["evidence_json"],
            tables["supervision_checkpoints"].c["eligible_since"],
        )
        .select_from(tables["supervision_checkpoints"])
        .where(
            and_(
                tables["supervision_checkpoints"].c["scope_type"] == bindparam("p0"),
                tables["supervision_checkpoints"].c["scope_id"] == bindparam("p1"),
                tables["supervision_checkpoints"].c["rule_id"] != sql_constant("admission"),
            )
        )
        .correlate_except(tables["supervision_checkpoints"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": scope_type, "p1": scope_id})


def find_for_is_current(conn, scope_type, scope):
    query1 = (
        select(tables["supervision_checkpoints"].c["metadata_json"])
        .select_from(tables["supervision_checkpoints"])
        .where(
            and_(
                tables["supervision_checkpoints"].c["scope_type"] == bindparam("p0"),
                tables["supervision_checkpoints"].c["scope_id"] == bindparam("p1"),
                tables["supervision_checkpoints"].c["rule_id"] == sql_constant("admission"),
            )
        )
        .correlate_except(tables["supervision_checkpoints"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": scope_type, "p1": scope})


def find_for_targets(conn, cutoff):
    table1 = tables["supervision_checkpoints"].alias("c")
    table2 = tables["tasks"].alias("t")
    table3 = tables["supervision_checkpoints"].alias("d")
    query4 = (
        select(
            table1.c["scope_type"],
            table1.c["scope_id"],
            table1.c["rule_id"],
            table1.c["lineage_key"],
        )
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["scope_id"]), isouter=False).join(
                table3,
                and_(
                    table3.c["scope_type"] == sql_constant("diagnosis"),
                    table3.c["scope_id"]
                    == sql_constant("candidate:")
                    .concat(table1.c["scope_id"])
                    .concat(sql_constant(":"))
                    .concat(table1.c["rule_id"]),
                    table3.c["rule_id"] == sql_constant("analysis"),
                ),
                isouter=True,
            )
        )
        .where(
            and_(
                table1.c["scope_type"] == sql_constant("task"),
                table1.c["classification"] == sql_constant("suspect"),
                table2.c["lifecycle"] == sql_constant(TaskLifecycle.RUNNING.value),
                func.coalesce(
                    func.json_extract(table3.c["metadata_json"], sql_constant("$.evidence_retry_at")),
                    sql_constant(""),
                )
                <= bindparam("p0"),
            )
        )
        .order_by(
            func.coalesce(table3.c["updated_at"], sql_constant("")).asc(),
            table1.c["last_progress_at"].asc(),
            table1.c["scope_id"].asc(),
            table1.c["rule_id"].asc(),
        )
        .limit(sql_constant(32))
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": cutoff})


def find_for_read_subject(conn, scope_type, scope_id, rule_id):
    table1 = tables["supervision_checkpoints"].alias("c")
    table2 = tables["tasks"].alias("t")
    query3 = (
        select(
            table1.c["scope_type"],
            table1.c["scope_id"],
            table1.c["rule_id"],
            table1.c["lineage_key"],
            table1.c["classification"].label("status"),
            table1.c["reason_code"],
            case(
                (
                    (func.length(table1.c["evidence_json"]) <= sql_constant(12000)),
                    table1.c["evidence_json"],
                )
            ).label("evidence_json"),
            null().label("expected_json"),
        )
        .select_from(table1.join(table2, (table2.c["task_id"] == table1.c["scope_id"]), isouter=False))
        .where(
            and_(
                table1.c["scope_type"] == bindparam("p0"),
                table1.c["scope_id"] == bindparam("p1"),
                table1.c["rule_id"] == bindparam("p2"),
                table2.c["lifecycle"] == sql_constant(TaskLifecycle.RUNNING.value),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": scope_type, "p1": scope_id, "p2": rule_id})


def find_for_candidate(conn, repository, policy_digest, value_3, value_4):
    table1 = tables["supervision_checkpoints"].alias("link")
    table2 = tables["work_items"].alias("item")
    table3 = tables["repos"].alias("repo")
    table4 = tables["worker_results"].alias("result")
    table5 = tables["tasks"].alias("t")
    table6 = tables["worker_results"].alias("wr")
    table7 = tables["attempts"].alias("a")
    query8 = (
        select(table6.c["result_id"])
        .select_from(
            table5.join(table6, (table6.c["task_id"] == table5.c["task_id"]), isouter=False).join(
                table7,
                (table7.c["attempt_id"] == table6.c["attempt_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table5.c["workstream_id"] == table2.c["workstream_id"],
                table6.c["output_type"] == sql_constant(OutputType.NEW_PR.value),
                table7.c["status"] == sql_constant(AttemptStatus.COMPLETED.value),
                func.json_extract(table6.c["metadata_json"], sql_constant("$.audit.status"))
                == sql_constant("accepted"),
                or_(
                    func.json_type(table7.c["metadata_json"], sql_constant("$.dispatch.pid")).is_not(
                        sql_constant("integer")
                    ),
                    not_(
                        func.json_extract(
                            table7.c["metadata_json"],
                            sql_constant("$.process_stopped_at"),
                        ).is_(null())
                    ),
                ),
            )
        )
        .order_by(table6.c["created_at"].desc(), table6.c["result_id"].desc())
        .limit(sql_constant(1))
        .correlate_except(table5, table6, table7)
        .set_label_style(LABEL_STYLE_NONE)
    )
    table9 = tables["attempts"].alias("attempt")
    table10 = tables["supervision_checkpoints"].alias("followup")
    query11 = (
        select(
            table1.c["scope_id"].label("incident_id"),
            table4.c["result_id"],
            table9.c["worktree_path"],
            func.json_extract(
                table4.c["metadata_json"],
                sql_constant("$.audit.action_scope.result_head_sha"),
            ).label("revision"),
        )
        .select_from(
            table1.join(
                table2,
                (
                    table2.c["work_item_id"]
                    == func.json_extract(table1.c["metadata_json"], sql_constant("$.work_item_id"))
                ),
                isouter=False,
            )
            .join(table3, (table3.c["repo_id"] == table2.c["repo_id"]), isouter=False)
            .join(
                table4,
                (table4.c["result_id"] == query8.scalar_subquery()),
                isouter=False,
            )
            .join(
                table9,
                (table9.c["attempt_id"] == table4.c["attempt_id"]),
                isouter=False,
            )
            .join(
                table10,
                and_(
                    table10.c["scope_type"] == sql_constant("repair"),
                    table10.c["scope_id"] == table1.c["scope_id"],
                    table10.c["rule_id"] == sql_constant("followup"),
                ),
                isouter=True,
            )
        )
        .where(
            and_(
                table1.c["scope_type"] == sql_constant("repair"),
                table1.c["rule_id"] == sql_constant("engineering"),
                table2.c["created_by"] == sql_constant("system:supervisor"),
                table2.c["canceled_at"].is_(null()),
                table3.c["enabled"] == sql_constant(1),
                func.lower(table3.c["full_name"]) == bindparam("p0"),
                func.coalesce(table9.c["worktree_path"], sql_constant("")) != sql_constant(""),
                or_(
                    or_(
                        or_(
                            func.coalesce(
                                func.json_extract(table10.c["metadata_json"], sql_constant("$.status")),
                                sql_constant(""),
                            )
                            != sql_constant("complete"),
                            func.json_extract(table10.c["metadata_json"], sql_constant("$.result_id")).is_not(
                                table4.c["result_id"]
                            ),
                        ),
                        func.json_extract(table10.c["metadata_json"], sql_constant("$.revision")).is_not(
                            func.json_extract(
                                table4.c["metadata_json"],
                                sql_constant("$.audit.action_scope.result_head_sha"),
                            )
                        ),
                    ),
                    func.json_extract(table10.c["metadata_json"], sql_constant("$.policy_digest")).is_not(
                        bindparam("p1")
                    ),
                ),
                or_(
                    func.json_extract(table10.c["metadata_json"], sql_constant("$.next_check_at")).is_(null()),
                    func.julianday(func.json_extract(table10.c["metadata_json"], sql_constant("$.next_check_at")))
                    <= func.julianday(bindparam("p2")),
                ),
                or_(
                    func.json_extract(table10.c["metadata_json"], sql_constant("$.claim_expires_at")).is_(null()),
                    func.julianday(
                        func.json_extract(
                            table10.c["metadata_json"],
                            sql_constant("$.claim_expires_at"),
                        )
                    )
                    <= func.julianday(bindparam("p3")),
                ),
            )
        )
        .order_by(
            func.coalesce(
                func.json_extract(table10.c["metadata_json"], sql_constant("$.next_check_at")),
                table1.c["updated_at"],
            ).asc(),
            table1.c["scope_id"].asc(),
        )
        .limit(sql_constant(1))
        .correlate_except(table1, table2, table3, table4, table9, table10)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(
        conn,
        query11,
        {"p0": repository, "p1": policy_digest, "p2": value_3, "p3": value_4},
    )


def find_for_get_checkpoint(conn, scope_type, scope_id, rule_id):
    query1 = (
        select(tables["supervision_checkpoints"])
        .select_from(tables["supervision_checkpoints"])
        .where(
            and_(
                tables["supervision_checkpoints"].c["scope_type"] == bindparam("p0"),
                tables["supervision_checkpoints"].c["scope_id"] == bindparam("p1"),
                tables["supervision_checkpoints"].c["rule_id"] == bindparam("p2"),
            )
        )
        .correlate_except(tables["supervision_checkpoints"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": scope_type, "p1": scope_id, "p2": rule_id})
