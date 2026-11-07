"""Shared queries operations for supervision_incidents."""

from robert_agent.foundation.contracts.supervision import IncidentStatus

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import (
    select,
    column,
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


def find_for_reserve(conn, task_id, lineage, rule):
    query1 = (
        select(tables["supervision_incidents"].c["incident_id"])
        .select_from(tables["supervision_incidents"])
        .where(
            and_(
                tables["supervision_incidents"].c["scope_type"] == sql_constant("task"),
                tables["supervision_incidents"].c["scope_id"] == bindparam("p0"),
                tables["supervision_incidents"].c["lineage_key"] == bindparam("p1"),
                tables["supervision_incidents"].c["rule_id"] == bindparam("p2"),
                not_(
                    tables["supervision_incidents"]
                    .c["status"]
                    .in_([sql_constant(IncidentStatus.RESOLVED.value), sql_constant(IncidentStatus.DISMISSED.value)])
                ),
            )
        )
        .correlate_except(tables["supervision_incidents"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id, "p1": lineage, "p2": rule})


def find_for_execution_recovery(conn, task_id, lineage):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["supervision_incidents"])
        .where(
            and_(
                tables["supervision_incidents"].c["scope_type"] == sql_constant("task"),
                tables["supervision_incidents"].c["scope_id"] == bindparam("p0"),
                tables["supervision_incidents"].c["rule_id"] == sql_constant("execution_failed"),
                tables["supervision_incidents"].c["lineage_key"] == bindparam("p1"),
                tables["supervision_incidents"].c["status"] != sql_constant(IncidentStatus.DISMISSED.value),
            )
        )
        .correlate_except(tables["supervision_incidents"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id, "p1": lineage})


def find_for_targets(conn, cutoff):
    table1 = tables["supervision_incidents"].alias("i")
    table2 = tables["supervision_checkpoints"].alias("c")
    query3 = (
        select(
            table1.c["incident_id"],
            table1.c["scope_type"],
            table1.c["scope_id"],
            table1.c["rule_id"],
            table1.c["lineage_key"],
        )
        .select_from(
            table1.join(
                table2,
                and_(
                    table2.c["scope_type"] == sql_constant("diagnosis"),
                    table2.c["scope_id"] == table1.c["incident_id"],
                    table2.c["rule_id"] == sql_constant("analysis"),
                ),
                isouter=True,
            )
        )
        .where(
            and_(
                table1.c["status"].in_(
                    [
                        sql_constant(IncidentStatus.OPEN.value),
                        sql_constant(IncidentStatus.NEEDS_ATTENTION.value),
                        sql_constant(IncidentStatus.DIAGNOSING.value),
                    ]
                ),
                (
                    func.coalesce(
                        func.json_extract(
                            table2.c["metadata_json"],
                            sql_constant("$.evidence_retry_at"),
                        ),
                        sql_constant(""),
                    )
                    <= bindparam("p0")
                ),
            )
        )
        .order_by(
            func.coalesce(table2.c["updated_at"], sql_constant("")).asc(),
            table1.c["first_observed_at"].asc(),
            table1.c["incident_id"].asc(),
        )
        .limit(sql_constant(32))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": cutoff})


def find_for_read_subject(conn, id):
    query1 = (
        select(
            tables["supervision_incidents"].c["scope_type"],
            tables["supervision_incidents"].c["scope_id"],
            tables["supervision_incidents"].c["rule_id"],
            tables["supervision_incidents"].c["lineage_key"],
            tables["supervision_incidents"].c["status"],
            tables["supervision_incidents"].c["reason_code"],
            case(
                (
                    (func.length(tables["supervision_incidents"].c["evidence_json"]) <= sql_constant(12000)),
                    tables["supervision_incidents"].c["evidence_json"],
                )
            ).label("evidence_json"),
            case(
                (
                    (func.length(tables["supervision_incidents"].c["expected_progress_json"]) <= sql_constant(3000)),
                    tables["supervision_incidents"].c["expected_progress_json"],
                )
            ).label("expected_json"),
        )
        .select_from(tables["supervision_incidents"])
        .where((tables["supervision_incidents"].c["incident_id"] == bindparam("p0")))
        .correlate_except(tables["supervision_incidents"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": id})


def find_record_by_incident_id(conn, incident_id):
    query1 = (
        select(tables["supervision_incidents"])
        .select_from(tables["supervision_incidents"])
        .where((tables["supervision_incidents"].c["incident_id"] == bindparam("p0")))
        .correlate_except(tables["supervision_incidents"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": incident_id})


def find_for_poll(conn):
    table1 = tables["supervision_incidents"].alias("i")
    table2 = tables["supervision_checkpoints"].alias("d")
    table3 = tables["supervision_checkpoints"].alias("r")
    query4 = (
        select(table1.c["incident_id"])
        .select_from(
            table1.join(
                table2,
                and_(
                    table2.c["scope_type"] == sql_constant("diagnosis"),
                    table2.c["scope_id"] == table1.c["incident_id"],
                    table2.c["rule_id"] == sql_constant("analysis"),
                ),
                isouter=False,
            ).join(
                table3,
                and_(
                    table3.c["scope_type"] == sql_constant("repair"),
                    table3.c["scope_id"] == table1.c["incident_id"],
                    table3.c["rule_id"] == sql_constant("engineering"),
                ),
                isouter=True,
            )
        )
        .where(
            and_(
                table1.c["status"].in_(
                    [
                        sql_constant(IncidentStatus.OPEN.value),
                        sql_constant(IncidentStatus.NEEDS_ATTENTION.value),
                        sql_constant(IncidentStatus.DIAGNOSING.value),
                    ]
                ),
                table3.c["scope_id"].is_(null()),
                func.json_extract(table2.c["metadata_json"], sql_constant("$.status")) == sql_constant("succeeded"),
                func.json_extract(
                    table2.c["metadata_json"],
                    sql_constant("$.diagnosis.diagnosis_class"),
                )
                == sql_constant("product_defect"),
                func.json_extract(
                    table2.c["metadata_json"],
                    sql_constant("$.diagnosis.recommended_action"),
                )
                == sql_constant("repair_task"),
            )
        )
        .order_by(table2.c["updated_at"].asc(), table1.c["incident_id"].asc())
        .limit(sql_constant(16))
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {})


def find_for_confirmed(conn, value_1, value_2, value_3, value_4):
    query1 = (
        select(tables["supervision_incidents"].c["incident_id"])
        .select_from(tables["supervision_incidents"])
        .where(
            and_(
                tables["supervision_incidents"].c["scope_type"] == bindparam("p0"),
                tables["supervision_incidents"].c["scope_id"] == bindparam("p1"),
                tables["supervision_incidents"].c["rule_id"] == bindparam("p2"),
                tables["supervision_incidents"].c["lineage_key"] == bindparam("p3"),
            )
        )
        .order_by(
            tables["supervision_incidents"].c["first_observed_at"].desc(),
            column("rowid").desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(tables["supervision_incidents"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": value_1, "p1": value_2, "p2": value_3, "p3": value_4})


def find_for_apply_observations(conn, value_1, value_2, value_3, value_4):
    query1 = (
        select(
            tables["supervision_incidents"].c["incident_id"],
            tables["supervision_incidents"].c["status"],
            tables["supervision_incidents"].c["expected_progress_json"],
            tables["supervision_incidents"].c["first_observed_at"],
        )
        .select_from(tables["supervision_incidents"])
        .where(
            and_(
                tables["supervision_incidents"].c["scope_type"] == bindparam("p0"),
                tables["supervision_incidents"].c["scope_id"] == bindparam("p1"),
                tables["supervision_incidents"].c["rule_id"] == bindparam("p2"),
                tables["supervision_incidents"].c["lineage_key"] == bindparam("p3"),
                not_(
                    tables["supervision_incidents"]
                    .c["status"]
                    .in_([sql_constant(IncidentStatus.RESOLVED.value), sql_constant(IncidentStatus.DISMISSED.value)])
                ),
            )
        )
        .correlate_except(tables["supervision_incidents"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": value_1, "p1": value_2, "p2": value_3, "p3": value_4})


def list_incidents(conn, status=None, limit=100):
    query1 = (
        select(tables["supervision_incidents"])
        .select_from(tables["supervision_incidents"])
        .order_by(
            tables["supervision_incidents"].c["last_observed_at"].desc(),
            tables["supervision_incidents"].c["incident_id"].desc(),
        )
        .limit(bindparam("p0"))
        .correlate_except(tables["supervision_incidents"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    if status == "active":
        query1 = query1.where(
            tables["supervision_incidents"].c.status.not_in(
                (sql_constant(IncidentStatus.RESOLVED.value), sql_constant(IncidentStatus.DISMISSED.value))
            )
        )
    elif status is not None:
        query1 = query1.where(tables["supervision_incidents"].c.status == status)
    return statements.read(conn, query1, {"p0": limit})
