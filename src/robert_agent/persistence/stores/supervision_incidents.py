"""Shared stores operations for supervision_incidents."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, bindparam, and_, not_, null
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def update_for_attention(conn, reason, now, incident_id):
    stmt = (
        update(tables["supervision_incidents"])
        .values(
            {
                "status": sql_constant("needs_attention"),
                "reason_code": bindparam("p0"),
                "last_observed_at": bindparam("p1"),
            }
        )
        .where(
            and_(
                (tables["supervision_incidents"].c["incident_id"] == bindparam("p2")),
                not_(
                    tables["supervision_incidents"]
                    .c["status"]
                    .in_([sql_constant("resolved"), sql_constant("dismissed")])
                ),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": reason, "p1": now, "p2": incident_id})


def update_for_begin(conn, incident_id):
    stmt = (
        update(tables["supervision_incidents"])
        .values({"status": sql_constant("recovering")})
        .where(
            and_(
                (tables["supervision_incidents"].c["incident_id"] == bindparam("p0")),
                tables["supervision_incidents"]
                .c["status"]
                .in_(
                    [
                        sql_constant("open"),
                        sql_constant("diagnosing"),
                        sql_constant("needs_attention"),
                    ]
                ),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": incident_id})


def update_for_finish(conn, incident_id):
    stmt = (
        update(tables["supervision_incidents"])
        .values({"status": sql_constant("verifying")})
        .where(
            and_(
                (tables["supervision_incidents"].c["incident_id"] == bindparam("p0")),
                not_(
                    tables["supervision_incidents"]
                    .c["status"]
                    .in_([sql_constant("resolved"), sql_constant("dismissed")])
                ),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": incident_id})


def update_for_verify(conn, now, now_2, value_3, incident_id):
    stmt = (
        update(tables["supervision_incidents"])
        .values(
            {
                "status": sql_constant("resolved"),
                "resolved_by": sql_constant("supervisor"),
                "resolved_at": bindparam("p0"),
                "last_observed_at": bindparam("p1"),
                "next_check_at": null(),
                "resolution_evidence_json": bindparam("p2"),
            }
        )
        .where(
            and_(
                (tables["supervision_incidents"].c["incident_id"] == bindparam("p3")),
                not_(
                    tables["supervision_incidents"]
                    .c["status"]
                    .in_([sql_constant("resolved"), sql_constant("dismissed")])
                ),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": now_2, "p2": value_3, "p3": incident_id})


def update_for_finish_2(conn, incident_id):
    stmt = (
        update(tables["supervision_incidents"])
        .values({"status": sql_constant("open")})
        .where(
            and_(
                (tables["supervision_incidents"].c["incident_id"] == bindparam("p0")),
                (tables["supervision_incidents"].c["status"] == sql_constant("recovering")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": incident_id})


def update_for_verify_2(conn, incident_id):
    stmt = (
        update(tables["supervision_incidents"])
        .values({"status": sql_constant("open")})
        .where(
            and_(
                (tables["supervision_incidents"].c["incident_id"] == bindparam("p0")),
                (tables["supervision_incidents"].c["status"] == sql_constant("verifying")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": incident_id})


def update_for_apply_observations(conn, now, now_2, value_3, incident_id):
    stmt = (
        update(tables["supervision_incidents"])
        .values(
            {
                "status": sql_constant("resolved"),
                "resolved_by": sql_constant("natural"),
                "resolved_at": bindparam("p0"),
                "last_observed_at": bindparam("p1"),
                "next_check_at": null(),
                "resolution_evidence_json": bindparam("p2"),
            }
        )
        .where((tables["supervision_incidents"].c["incident_id"] == bindparam("p3")))
    )
    return statements.write(conn, stmt, {"p0": now, "p1": now_2, "p2": value_3, "p3": incident_id})


def insert_for_confirmed(
    conn,
    value_1,
    value_2,
    value_3,
    value_4,
    value_5,
    value_6,
    value_7,
    value_8,
    value_9,
    value_10,
    value_11,
    value_12,
    value_13,
):
    stmt = insert(tables["supervision_incidents"]).values(
        {
            "incident_id": bindparam("p0"),
            "scope_type": bindparam("p1"),
            "scope_id": bindparam("p2"),
            "rule_id": bindparam("p3"),
            "lineage_key": bindparam("p4"),
            "status": sql_constant("open"),
            "reason_code": bindparam("p5"),
            "severity": bindparam("p6"),
            "evidence_json": bindparam("p7"),
            "expected_progress_json": bindparam("p8"),
            "last_observed_at": bindparam("p9"),
            "next_check_at": bindparam("p10"),
            "first_observed_at": bindparam("p11"),
            "prior_incident_id": bindparam("p12"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": value_1,
            "p1": value_2,
            "p2": value_3,
            "p3": value_4,
            "p4": value_5,
            "p5": value_6,
            "p6": value_7,
            "p7": value_8,
            "p8": value_9,
            "p9": value_10,
            "p10": value_11,
            "p11": value_12,
            "p12": value_13,
        },
    )


def update_for_confirmed(conn, value_1, value_2, value_3, value_4, value_5, value_6, value_7):
    stmt = (
        update(tables["supervision_incidents"])
        .values(
            {
                "reason_code": bindparam("p0"),
                "severity": bindparam("p1"),
                "evidence_json": bindparam("p2"),
                "expected_progress_json": bindparam("p3"),
                "last_observed_at": bindparam("p4"),
                "next_check_at": bindparam("p5"),
            }
        )
        .where((tables["supervision_incidents"].c["incident_id"] == bindparam("p6")))
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": value_1,
            "p1": value_2,
            "p2": value_3,
            "p3": value_4,
            "p4": value_5,
            "p5": value_6,
            "p6": value_7,
        },
    )
