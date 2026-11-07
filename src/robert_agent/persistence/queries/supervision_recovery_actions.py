"""Shared queries operations for supervision_recovery_actions."""

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


def find_action_kind_and_status_and_budget_cost_by_incident_id(conn, incident_id):
    query1 = (
        select(
            tables["supervision_recovery_actions"].c["action_kind"],
            tables["supervision_recovery_actions"].c["status"],
            tables["supervision_recovery_actions"].c["budget_cost"],
        )
        .select_from(tables["supervision_recovery_actions"])
        .where((tables["supervision_recovery_actions"].c["incident_id"] == bindparam("p0")))
        .order_by(tables["supervision_recovery_actions"].c["created_at"].desc())
        .limit(sql_constant(10))
        .correlate_except(tables["supervision_recovery_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": incident_id})


def find_for_budget_block(conn, stage, lineage):
    query1 = (
        select(
            func.coalesce(
                func.sum(tables["supervision_recovery_actions"].c["budget_cost"]),
                sql_constant(0),
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            (tables["supervision_recovery_actions"].c["stage_family"] == bindparam("p0")),
                            tables["supervision_recovery_actions"].c["budget_cost"],
                        ),
                        else_=sql_constant(0),
                    )
                ),
                sql_constant(0),
            ),
        )
        .select_from(tables["supervision_recovery_actions"])
        .where((tables["supervision_recovery_actions"].c["lineage_key"] == bindparam("p1")))
        .correlate_except(tables["supervision_recovery_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": stage, "p1": lineage})


def find_for_reserve(conn, lineage, task_id, kind):
    query1 = (
        select(
            (
                func.coalesce(
                    func.max(tables["supervision_recovery_actions"].c["recovery_ordinal"]),
                    sql_constant(0),
                )
                + sql_constant(1)
            )
        )
        .select_from(tables["supervision_recovery_actions"])
        .where(
            and_(
                tables["supervision_recovery_actions"].c["lineage_key"] == bindparam("p0"),
                tables["supervision_recovery_actions"].c["scope_type"] == sql_constant("task"),
                tables["supervision_recovery_actions"].c["scope_id"] == bindparam("p1"),
                tables["supervision_recovery_actions"].c["action_kind"] == bindparam("p2"),
            )
        )
        .correlate_except(tables["supervision_recovery_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": lineage, "p1": task_id, "p2": kind})


def find_for_begin(conn, task_id):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["supervision_recovery_actions"])
        .where(
            and_(
                tables["supervision_recovery_actions"].c["scope_type"] == sql_constant("task"),
                tables["supervision_recovery_actions"].c["scope_id"] == bindparam("p0"),
                not_(
                    tables["supervision_recovery_actions"]
                    .c["action_kind"]
                    .in_(
                        [
                            sql_constant("publication_retry"),
                            sql_constant("publication_reconcile"),
                        ]
                    )
                ),
                tables["supervision_recovery_actions"]
                .c["status"]
                .in_(
                    [
                        sql_constant("requested"),
                        sql_constant("applying"),
                        sql_constant("observing"),
                        sql_constant("unknown"),
                    ]
                ),
            )
        )
        .correlate_except(tables["supervision_recovery_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_begin_2(conn, task_id, action_id):
    query1 = (
        select(tables["supervision_recovery_actions"])
        .select_from(tables["supervision_recovery_actions"])
        .where(
            and_(
                tables["supervision_recovery_actions"].c["scope_type"] == sql_constant("task"),
                tables["supervision_recovery_actions"].c["scope_id"] == bindparam("p0"),
                tables["supervision_recovery_actions"].c["target_id"] == bindparam("p1"),
                tables["supervision_recovery_actions"]
                .c["action_kind"]
                .in_(
                    [
                        sql_constant("publication_retry"),
                        sql_constant("publication_reconcile"),
                    ]
                ),
                tables["supervision_recovery_actions"]
                .c["status"]
                .in_(
                    [
                        sql_constant("applying"),
                        sql_constant("unknown"),
                        sql_constant("observing"),
                    ]
                ),
            )
        )
        .correlate_except(tables["supervision_recovery_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id, "p1": action_id})


def find_for_budget_block_2(conn, lineage):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["supervision_recovery_actions"])
        .where(
            and_(
                (tables["supervision_recovery_actions"].c["lineage_key"] == bindparam("p0")),
                tables["supervision_recovery_actions"]
                .c["status"]
                .in_(
                    [
                        sql_constant("requested"),
                        sql_constant("applying"),
                        sql_constant("observing"),
                        sql_constant("unknown"),
                    ]
                ),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(tables["supervision_recovery_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": lineage})


def find_for_blocks_consumer(conn, task_id):
    query1 = (
        select(
            tables["supervision_recovery_actions"].c["action_kind"],
            tables["supervision_recovery_actions"].c["target_id"],
            tables["supervision_recovery_actions"].c["status"],
        )
        .select_from(tables["supervision_recovery_actions"])
        .where(
            and_(
                tables["supervision_recovery_actions"].c["scope_type"] == sql_constant("task"),
                tables["supervision_recovery_actions"].c["scope_id"] == bindparam("p0"),
                tables["supervision_recovery_actions"]
                .c["status"]
                .in_(
                    [
                        sql_constant("requested"),
                        sql_constant("applying"),
                        sql_constant("observing"),
                        sql_constant("unknown"),
                    ]
                ),
            )
        )
        .correlate_except(tables["supervision_recovery_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_record_by_action_id(conn, action_id):
    query1 = (
        select(tables["supervision_recovery_actions"])
        .select_from(tables["supervision_recovery_actions"])
        .where((tables["supervision_recovery_actions"].c["action_id"] == bindparam("p0")))
        .correlate_except(tables["supervision_recovery_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": action_id})


def find_for_fenced(conn, action_id, claim_owner, claim_generation):
    query1 = (
        select(tables["supervision_recovery_actions"])
        .select_from(tables["supervision_recovery_actions"])
        .where(
            and_(
                tables["supervision_recovery_actions"].c["action_id"] == bindparam("p0"),
                tables["supervision_recovery_actions"].c["status"] == sql_constant("applying"),
                tables["supervision_recovery_actions"].c["claim_owner"] == bindparam("p1"),
                tables["supervision_recovery_actions"].c["claim_generation"] == bindparam("p2"),
            )
        )
        .correlate_except(tables["supervision_recovery_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": action_id, "p1": claim_owner, "p2": claim_generation})


def find_for_finish(conn, action_id, claim_owner, claim_generation):
    query1 = (
        select(tables["supervision_recovery_actions"])
        .select_from(tables["supervision_recovery_actions"])
        .where(
            and_(
                tables["supervision_recovery_actions"].c["action_id"] == bindparam("p0"),
                tables["supervision_recovery_actions"]
                .c["status"]
                .in_([sql_constant("applying"), sql_constant("unknown")]),
                tables["supervision_recovery_actions"].c["claim_owner"] == bindparam("p1"),
                tables["supervision_recovery_actions"].c["claim_generation"] == bindparam("p2"),
            )
        )
        .correlate_except(tables["supervision_recovery_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": action_id, "p1": claim_owner, "p2": claim_generation})


def find_for_verify(conn, now, value_2):
    query1 = (
        select(tables["supervision_recovery_actions"])
        .select_from(tables["supervision_recovery_actions"])
        .where(
            and_(
                tables["supervision_recovery_actions"]
                .c["status"]
                .in_(
                    [
                        sql_constant("requested"),
                        sql_constant("applying"),
                        sql_constant("observing"),
                        sql_constant("unknown"),
                    ]
                ),
                (
                    or_(
                        tables["supervision_recovery_actions"].c["next_check_at"].is_(null()),
                        (
                            func.julianday(tables["supervision_recovery_actions"].c["next_check_at"])
                            <= func.julianday(bindparam("p0"))
                        ),
                    )
                ),
            )
        )
        .order_by(
            tables["supervision_recovery_actions"].c["next_check_at"].asc(),
            tables["supervision_recovery_actions"].c["action_id"].asc(),
        )
        .limit(bindparam("p1"))
        .correlate_except(tables["supervision_recovery_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": now, "p1": value_2})


def find_for_begin_3(conn, task_id):
    query1 = (
        select(tables["supervision_recovery_actions"])
        .select_from(tables["supervision_recovery_actions"])
        .where(
            and_(
                tables["supervision_recovery_actions"].c["scope_type"] == sql_constant("task"),
                tables["supervision_recovery_actions"].c["scope_id"] == bindparam("p0"),
                tables["supervision_recovery_actions"]
                .c["status"]
                .in_(
                    [
                        sql_constant("requested"),
                        sql_constant("applying"),
                        sql_constant("observing"),
                        sql_constant("unknown"),
                    ]
                ),
            )
        )
        .correlate_except(tables["supervision_recovery_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_begin_4(conn, task_id):
    query1 = (
        select(tables["supervision_recovery_actions"].c["action_id"])
        .select_from(tables["supervision_recovery_actions"])
        .where(
            and_(
                tables["supervision_recovery_actions"].c["scope_type"] == sql_constant("task"),
                tables["supervision_recovery_actions"].c["scope_id"] == bindparam("p0"),
                tables["supervision_recovery_actions"]
                .c["status"]
                .in_(
                    [
                        sql_constant("requested"),
                        sql_constant("applying"),
                        sql_constant("observing"),
                        sql_constant("unknown"),
                    ]
                ),
            )
        )
        .correlate_except(tables["supervision_recovery_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})
