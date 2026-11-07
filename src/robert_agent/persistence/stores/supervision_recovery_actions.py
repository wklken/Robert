"""Shared stores operations for supervision_recovery_actions."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, bindparam, and_, null, case
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_for_reserve(
    conn,
    action_id,
    value_2,
    lineage,
    task_id,
    stage,
    kind,
    ordinal,
    key,
    target_id,
    version,
    policy,
    value_12,
    value_13,
    now,
    value_15,
):
    stmt = insert(tables["supervision_recovery_actions"]).values(
        {
            "action_id": bindparam("p0"),
            "incident_id": bindparam("p1"),
            "lineage_key": bindparam("p2"),
            "scope_type": sql_constant("task"),
            "scope_id": bindparam("p3"),
            "stage_family": bindparam("p4"),
            "action_kind": bindparam("p5"),
            "recovery_ordinal": bindparam("p6"),
            "idempotency_key": bindparam("p7"),
            "target_id": bindparam("p8"),
            "target_generation": bindparam("p9"),
            "policy_version": bindparam("p10"),
            "status": sql_constant("requested"),
            "budget_cost": sql_constant(1),
            "preconditions_json": bindparam("p11"),
            "expected_progress_json": bindparam("p12"),
            "created_at": bindparam("p13"),
            "next_check_at": bindparam("p14"),
        }
    )
    stmt = stmt.on_conflict_do_nothing()
    return statements.write(
        conn,
        stmt,
        {
            "p0": action_id,
            "p1": value_2,
            "p2": lineage,
            "p3": task_id,
            "p4": stage,
            "p5": kind,
            "p6": ordinal,
            "p7": key,
            "p8": target_id,
            "p9": version,
            "p10": policy,
            "p11": value_12,
            "p12": value_13,
            "p13": now,
            "p14": value_15,
        },
    )


def update_for_begin(conn, owner, value_2, now, value_4, action_id, claim_generation):
    stmt = (
        update(tables["supervision_recovery_actions"])
        .values(
            {
                "status": sql_constant("applying"),
                "claim_owner": bindparam("p0"),
                "claim_generation": (tables["supervision_recovery_actions"].c["claim_generation"] + sql_constant(1)),
                "claim_expires_at": bindparam("p1"),
                "started_at": bindparam("p2"),
                "next_check_at": bindparam("p3"),
            }
        )
        .where(
            and_(
                tables["supervision_recovery_actions"].c["action_id"] == bindparam("p4"),
                tables["supervision_recovery_actions"].c["status"] == sql_constant("requested"),
                tables["supervision_recovery_actions"].c["claim_generation"] == bindparam("p5"),
            )
        )
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": owner,
            "p1": value_2,
            "p2": now,
            "p3": value_4,
            "p4": action_id,
            "p5": claim_generation,
        },
    )


def update_for_finish(
    conn,
    outcome,
    value_2,
    now,
    value_4,
    outcome_5,
    action_id,
    status,
    claim_owner,
    claim_generation,
):
    stmt = (
        update(tables["supervision_recovery_actions"])
        .values(
            {
                "status": bindparam("p0"),
                "receipt_json": bindparam("p1"),
                "finished_at": bindparam("p2"),
                "next_check_at": bindparam("p3"),
                "budget_cost": case(
                    ((bindparam("p4") == sql_constant("rejected")), sql_constant(0)),
                    else_=tables["supervision_recovery_actions"].c["budget_cost"],
                ),
            }
        )
        .where(
            and_(
                tables["supervision_recovery_actions"].c["action_id"] == bindparam("p5"),
                tables["supervision_recovery_actions"].c["status"] == bindparam("p6"),
                tables["supervision_recovery_actions"].c["claim_owner"] == bindparam("p7"),
                tables["supervision_recovery_actions"].c["claim_generation"] == bindparam("p8"),
            )
        )
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": outcome,
            "p1": value_2,
            "p2": now,
            "p3": value_4,
            "p4": outcome_5,
            "p5": action_id,
            "p6": status,
            "p7": claim_owner,
            "p8": claim_generation,
        },
    )


def update_for_begin_2(conn, now, action_id):
    stmt = (
        update(tables["supervision_recovery_actions"])
        .values(
            {
                "status": sql_constant("rejected"),
                "budget_cost": sql_constant(0),
                "finished_at": bindparam("p0"),
                "next_check_at": null(),
            }
        )
        .where(
            and_(
                (tables["supervision_recovery_actions"].c["action_id"] == bindparam("p1")),
                (tables["supervision_recovery_actions"].c["status"] == sql_constant("requested")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": action_id})


def update_for_verify(conn, now, value_2, action_id, state):
    stmt = (
        update(tables["supervision_recovery_actions"])
        .values(
            {
                "status": sql_constant("succeeded"),
                "finished_at": bindparam("p0"),
                "next_check_at": null(),
                "verification_evidence_json": bindparam("p1"),
                "budget_cost": case(
                    (
                        (tables["supervision_recovery_actions"].c["status"] == sql_constant("requested")),
                        sql_constant(0),
                    ),
                    else_=tables["supervision_recovery_actions"].c["budget_cost"],
                ),
            }
        )
        .where(
            and_(
                (tables["supervision_recovery_actions"].c["action_id"] == bindparam("p2")),
                (tables["supervision_recovery_actions"].c["status"] == bindparam("p3")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": value_2, "p2": action_id, "p3": state})


def update_for_verify_2(conn, outcome, value_2, now, outcome_4, action_id, state):
    stmt = (
        update(tables["supervision_recovery_actions"])
        .values(
            {
                "status": bindparam("p0"),
                "next_check_at": bindparam("p1"),
                "finished_at": bindparam("p2"),
                "budget_cost": case(
                    ((bindparam("p3") == sql_constant("rejected")), sql_constant(0)),
                    else_=tables["supervision_recovery_actions"].c["budget_cost"],
                ),
            }
        )
        .where(
            and_(
                (tables["supervision_recovery_actions"].c["action_id"] == bindparam("p4")),
                (tables["supervision_recovery_actions"].c["status"] == bindparam("p5")),
            )
        )
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": outcome,
            "p1": value_2,
            "p2": now,
            "p3": outcome_4,
            "p4": action_id,
            "p5": state,
        },
    )


def set_next_check_at_by_action_id(conn, value_1, action_id):
    stmt = (
        update(tables["supervision_recovery_actions"])
        .values({"next_check_at": bindparam("p0")})
        .where((tables["supervision_recovery_actions"].c["action_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": action_id})
