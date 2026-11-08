"""Shared queries operations for github_events."""

from robert_agent.foundation.contracts.work import WorkstreamLifecycle

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import (
    select,
    literal_column,
    bindparam,
    and_,
    or_,
    not_,
    null,
    func,
    LABEL_STYLE_NONE,
)


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def load_priority_pending_events(conn, priority_limit):
    table1 = tables["github_events"].alias("ge")
    table2 = tables["github_sources"].alias("gs")
    query3 = (
        select(
            table1.c["event_id"],
            table1.c["event_fingerprint"],
            table1.c["event_type"],
            table1.c["actor_login"],
            table1.c["author_association"],
            table1.c["authorization_status"],
            table1.c["event_at"],
            table1.c["payload_json"],
            table2.c["source_key"],
            table2.c["title"].label("source_title"),
            table2.c["source_type"],
            table2.c["number"],
            table2.c["html_url"],
            null().label("task_id"),
            null().label("task_relationship"),
            null().label("workstream_id"),
        )
        .select_from(table1.join(table2, (table2.c["source_id"] == table1.c["source_id"]), isouter=False))
        .where(
            table1.c["authorization_status"].in_(
                [
                    sql_constant("pending_actor_permission"),
                    sql_constant("pending_authorization"),
                ]
            )
        )
        .order_by(table1.c["event_at"].desc(), table1.c["event_id"].desc())
        .limit(bindparam("p0"))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": priority_limit})


def find_for_build_dashboard_data(conn, history_limit):
    table1 = tables["github_events"].alias("ge")
    table2 = tables["github_sources"].alias("gs")
    table3 = tables["task_events"].alias("te")
    table4 = tables["tasks"].alias("t")
    query5 = (
        select(
            table1.c["event_id"],
            table1.c["event_fingerprint"],
            table1.c["event_type"],
            table1.c["actor_login"],
            table1.c["author_association"],
            table1.c["authorization_status"],
            table1.c["event_at"],
            table1.c["payload_json"],
            table2.c["source_key"],
            table2.c["title"].label("source_title"),
            table2.c["source_type"],
            table2.c["number"],
            table2.c["html_url"],
            table3.c["task_id"],
            table3.c["relationship"].label("task_relationship"),
            table4.c["workstream_id"],
        )
        .select_from(
            table1.join(table2, (table2.c["source_id"] == table1.c["source_id"]), isouter=False)
            .join(table3, (table3.c["event_id"] == table1.c["event_id"]), isouter=True)
            .join(table4, (table4.c["task_id"] == table3.c["task_id"]), isouter=True)
        )
        .order_by(table1.c["event_at"].desc(), table1.c["event_id"].desc())
        .limit(bindparam("p0"))
        .correlate_except(table1, table2, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query5, {"p0": history_limit})


def find_for_build_events(conn, value_1, like, like_3, like_4, like_5, limit):
    table1 = tables["github_events"].alias("ge")
    table2 = tables["github_sources"].alias("gs")
    table3 = tables["task_events"].alias("te")
    query4 = (
        select(
            table1.c["event_id"],
            table1.c["event_fingerprint"],
            table1.c["event_type"],
            table1.c["actor_login"],
            table1.c["author_association"],
            table1.c["authorization_status"],
            table1.c["event_at"],
            table1.c["payload_json"],
            table2.c["source_key"],
            table2.c["source_type"],
            table2.c["number"],
            table2.c["html_url"],
            func.count(table3.c["task_id"]).label("linked_task_count"),
        )
        .select_from(
            table1.join(table2, (table2.c["source_id"] == table1.c["source_id"]), isouter=False).join(
                table3, (table3.c["event_id"] == table1.c["event_id"]), isouter=True
            )
        )
        .where(
            or_(
                table1.c["event_fingerprint"] == bindparam("p0"),
                table1.c["event_fingerprint"].like(bindparam("p1")),
                table1.c["payload_json"].like(bindparam("p2")),
                table2.c["source_key"].like(bindparam("p3")),
                table2.c["html_url"].like(bindparam("p4")),
            )
        )
        .group_by(table1.c["event_id"])
        .order_by(table1.c["event_at"].desc(), table1.c["event_id"].desc())
        .limit(bindparam("p5"))
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(
        conn,
        query4,
        {
            "p0": value_1,
            "p1": like,
            "p2": like_3,
            "p3": like_4,
            "p4": like_5,
            "p5": limit,
        },
    )


def find_for_build_event(conn, candidate):
    table1 = tables["github_events"].alias("ge")
    table2 = tables["github_sources"].alias("gs")
    query3 = (
        select(
            table1.c["event_id"],
            table1.c["event_fingerprint"],
            table1.c["event_type"],
            table1.c["actor_login"],
            table1.c["author_association"],
            table1.c["authorization_status"],
            table1.c["event_at"],
            table1.c["payload_json"],
            table2.c["source_key"],
            table2.c["source_type"],
            table2.c["number"],
            table2.c["html_url"],
            table2.c["title"],
            table2.c["state"],
        )
        .select_from(table1.join(table2, (table2.c["source_id"] == table1.c["source_id"]), isouter=False))
        .where((table1.c["event_fingerprint"] == bindparam("p0")))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": candidate})


def find_for_summarize_database(conn):
    query1 = (
        select(func.count(literal_column("*")))
        .select_from(tables["github_events"])
        .where((tables["github_events"].c["authorization_status"] == sql_constant("pending_actor_permission")))
        .correlate_except(tables["github_events"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_summarize_database_2(conn):
    query1 = (
        select(func.count(literal_column("*")))
        .select_from(tables["github_events"])
        .where((tables["github_events"].c["authorization_status"] == sql_constant("pending_authorization")))
        .correlate_except(tables["github_events"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_event_at_by_event_id(conn, value_1):
    query1 = (
        select(tables["github_events"].c["event_at"])
        .select_from(tables["github_events"])
        .where((tables["github_events"].c["event_id"] == bindparam("p0")))
        .correlate_except(tables["github_events"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": value_1})


def find_payload_json_by_event_fingerprint(conn, event_fingerprint):
    query1 = (
        select(tables["github_events"].c["payload_json"])
        .select_from(tables["github_events"])
        .where((tables["github_events"].c["event_fingerprint"] == bindparam("p0")))
        .correlate_except(tables["github_events"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": event_fingerprint})


def scan_repo_filtered(conn, items, value_1, value_2):
    table1 = tables["github_events"].alias("ge")
    table2 = tables["github_sources"].alias("gs")
    table3 = tables["workstreams"].alias("w")
    table5 = tables["task_events"].alias("te")
    query6 = (
        select(sql_constant(1))
        .select_from(table5)
        .where((table5.c["event_id"] == table1.c["event_id"]))
        .correlate_except(table5)
        .set_label_style(LABEL_STYLE_NONE)
    )
    table7 = tables["work_items"].alias("wi")
    query8 = (
        select(sql_constant(1))
        .select_from(table7)
        .where(
            and_(
                (table7.c["workstream_id"] == table3.c["workstream_id"]),
                not_(table7.c["canceled_at"].is_(null())),
            )
        )
        .correlate_except(table7)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query4 = (
        select(table1.c["payload_json"])
        .select_from(
            table1.join(table2, (table2.c["source_id"] == table1.c["source_id"]), isouter=False).join(
                table3,
                and_(
                    (table3.c["primary_source_id"] == table2.c["source_id"]),
                    (table3.c["repo_id"] == table1.c["repo_id"]),
                ),
                isouter=False,
            )
        )
        .where(
            and_(
                table1.c["repo_id"] == bindparam("p0"),
                table1.c["authorization_status"] == sql_constant("accepted_context"),
                func.lower(table1.c["actor_login"]).in_(bindparam("items", expanding=True)),
                func.lower(table1.c["actor_login"]) != bindparam("p1"),
                table1.c["actor_kind"] != sql_constant("github_system"),
                func.lower(table2.c["state"]) == sql_constant("open"),
                table3.c["lifecycle"].in_(
                    [sql_constant(WorkstreamLifecycle.COMPLETED.value), sql_constant(WorkstreamLifecycle.FAILED.value)]
                ),
                table3.c["active_task_id"].is_(null()),
                not_(query6.exists()),
                not_(query8.exists()),
            )
        )
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(
        conn, query4, {"p0": value_1, "p1": value_2.casefold(), "items": [login.casefold() for login in items]}
    )


def legacy_body_request(conn, repo_id, source_key, actor, body):
    """Keep an already observed body request's identity across collector upgrades."""
    event = tables["github_events"]
    query = (
        select(event.c.event_fingerprint)
        .where(
            event.c.repo_id == repo_id,
            event.c.event_type == "mention",
            event.c.event_fingerprint.not_like("%:body:%"),
            func.json_extract(event.c.payload_json, "$.source_key") == source_key,
            func.lower(event.c.actor_login) == actor.casefold(),
            func.json_extract(event.c.payload_json, "$.body") == body,
        )
        .order_by(event.c.event_at, event.c.event_id)
        .limit(1)
    )
    return statements.read(conn, query, {})
