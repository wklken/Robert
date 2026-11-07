"""Shared stores operations for github_events."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, bindparam, and_, not_, case
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_source_and_event(
    conn,
    event_id,
    repo_id,
    source_id,
    event_fingerprint,
    event_type,
    value_6,
    value_7,
    value_8,
    value_9,
    value_10,
    value_11,
):
    stmt = insert(tables["github_events"]).values(
        {
            "event_id": bindparam("p0"),
            "repo_id": bindparam("p1"),
            "source_id": bindparam("p2"),
            "event_fingerprint": bindparam("p3"),
            "event_type": bindparam("p4"),
            "actor_kind": bindparam("p5"),
            "actor_login": bindparam("p6"),
            "author_association": bindparam("p7"),
            "authorization_status": bindparam("p8"),
            "event_at": bindparam("p9"),
            "payload_json": bindparam("p10"),
        }
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["event_fingerprint"],
        set_={
            "authorization_status": case(
                (
                    tables["github_events"]
                    .c["authorization_status"]
                    .in_(
                        [
                            sql_constant("authorized_trigger"),
                            sql_constant("authorized_system_trigger"),
                        ]
                    ),
                    tables["github_events"].c["authorization_status"],
                ),
                else_=stmt.excluded["authorization_status"],
            ),
            "actor_kind": case(
                (
                    tables["github_events"]
                    .c["authorization_status"]
                    .in_(
                        [
                            sql_constant("authorized_trigger"),
                            sql_constant("authorized_system_trigger"),
                        ]
                    ),
                    tables["github_events"].c["actor_kind"],
                ),
                else_=stmt.excluded["actor_kind"],
            ),
            "author_association": case(
                (
                    and_(
                        tables["github_events"]
                        .c["authorization_status"]
                        .in_(
                            [
                                sql_constant("authorized_trigger"),
                                sql_constant("authorized_system_trigger"),
                            ]
                        ),
                        not_(
                            stmt.excluded["authorization_status"].in_(
                                [
                                    sql_constant("authorized_trigger"),
                                    sql_constant("authorized_system_trigger"),
                                ]
                            )
                        ),
                    ),
                    tables["github_events"].c["author_association"],
                ),
                else_=stmt.excluded["author_association"],
            ),
            "payload_json": case(
                (
                    and_(
                        tables["github_events"]
                        .c["authorization_status"]
                        .in_(
                            [
                                sql_constant("authorized_trigger"),
                                sql_constant("authorized_system_trigger"),
                            ]
                        ),
                        not_(
                            stmt.excluded["authorization_status"].in_(
                                [
                                    sql_constant("authorized_trigger"),
                                    sql_constant("authorized_system_trigger"),
                                ]
                            )
                        ),
                    ),
                    tables["github_events"].c["payload_json"],
                ),
                else_=stmt.excluded["payload_json"],
            ),
        },
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": event_id,
            "p1": repo_id,
            "p2": source_id,
            "p3": event_fingerprint,
            "p4": event_type,
            "p5": value_6,
            "p6": value_7,
            "p7": value_8,
            "p8": value_9,
            "p9": value_10,
            "p10": value_11,
        },
    )


def record_current_state_closed_source(conn, event_id):
    stmt = (
        update(tables["github_events"])
        .values({"authorization_status": sql_constant("ignored_current_state_closed")})
        .where((tables["github_events"].c["event_id"] == bindparam("p0")))
    )
    return statements.write(conn, stmt, {"p0": event_id})
