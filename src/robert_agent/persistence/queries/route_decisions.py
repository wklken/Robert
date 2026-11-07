"""Shared queries operations for route_decisions."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, bindparam, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_route_decision_id_by_task_id(conn, task_id):
    query1 = (
        select(tables["route_decisions"].c["route_decision_id"])
        .select_from(tables["route_decisions"])
        .where((tables["route_decisions"].c["task_id"] == bindparam("p0")))
        .limit(sql_constant(1))
        .correlate_except(tables["route_decisions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_latest_route_result_for_task(conn, task_id):
    query1 = (
        select(
            tables["route_decisions"].c["route_id"],
            tables["route_decisions"].c["expected_output"],
            tables["route_decisions"].c["allowed_github_actions_json"],
            tables["route_decisions"].c["required_skills_json"],
            tables["route_decisions"].c["recommended_skills_json"],
            tables["route_decisions"].c["confidence"],
        )
        .select_from(tables["route_decisions"])
        .where((tables["route_decisions"].c["task_id"] == bindparam("p0")))
        .order_by(
            tables["route_decisions"].c["created_at"].desc(),
            tables["route_decisions"].c["route_decision_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(tables["route_decisions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})
