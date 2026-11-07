"""Shared stores operations for route_decisions."""

from sqlalchemy import update, bindparam
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_for_upsert_route_decision(
    conn,
    value_1,
    task_id,
    route_id,
    expected_output,
    value_5,
    value_6,
    value_7,
    confidence,
    run_now,
):
    stmt = insert(tables["route_decisions"]).values(
        {
            "route_decision_id": bindparam("p0"),
            "task_id": bindparam("p1"),
            "route_id": bindparam("p2"),
            "expected_output": bindparam("p3"),
            "allowed_github_actions_json": bindparam("p4"),
            "required_skills_json": bindparam("p5"),
            "recommended_skills_json": bindparam("p6"),
            "confidence": bindparam("p7"),
            "created_at": bindparam("p8"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": value_1,
            "p1": task_id,
            "p2": route_id,
            "p3": expected_output,
            "p4": value_5,
            "p5": value_6,
            "p6": value_7,
            "p7": confidence,
            "p8": run_now,
        },
    )


def update_for_upsert_route_decision(
    conn,
    route_id,
    expected_output,
    value_3,
    value_4,
    value_5,
    confidence,
    run_now,
    value_8,
):
    stmt = (
        update(tables["route_decisions"])
        .values(
            {
                "route_id": bindparam("p0"),
                "expected_output": bindparam("p1"),
                "allowed_github_actions_json": bindparam("p2"),
                "required_skills_json": bindparam("p3"),
                "recommended_skills_json": bindparam("p4"),
                "confidence": bindparam("p5"),
                "created_at": bindparam("p6"),
            }
        )
        .where((tables["route_decisions"].c["route_decision_id"] == bindparam("p7")))
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": route_id,
            "p1": expected_output,
            "p2": value_3,
            "p3": value_4,
            "p4": value_5,
            "p5": confidence,
            "p6": run_now,
            "p7": value_8,
        },
    )
