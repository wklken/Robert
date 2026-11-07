"""Shared stores operations for vote_sessions."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, bindparam, and_, not_
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_for_enqueue(
    conn,
    round_id,
    full_name,
    number,
    source_type,
    workstream_id,
    trigger,
    value_7,
    timestamp,
    timestamp_9,
):
    stmt = (
        insert(tables["vote_sessions"])
        .values(
            {
                "round_id": bindparam("p0"),
                "repo": bindparam("p1"),
                "number": bindparam("p2"),
                "source_type": bindparam("p3"),
                "workstream_id": bindparam("p4"),
                "trigger_fingerprint": bindparam("p5"),
                "request_json": bindparam("p6"),
                "created_at": bindparam("p7"),
                "updated_at": bindparam("p8"),
            }
        )
        .prefix_with("OR IGNORE", dialect="sqlite")
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": round_id,
            "p1": full_name,
            "p2": number,
            "p3": source_type,
            "p4": workstream_id,
            "p5": trigger,
            "p6": value_7,
            "p7": timestamp,
            "p8": timestamp_9,
        },
    )


def set_status_and_updated_at_by_round_id(conn, status, now, vote_round_id):
    stmt = (
        update(tables["vote_sessions"])
        .values({"status": bindparam("p0"), "updated_at": bindparam("p1")})
        .where((tables["vote_sessions"].c["round_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": status, "p1": now, "p2": vote_round_id})


def cancel_execution_filtered(conn, items, value_1):
    stmt = (
        update(tables["vote_sessions"])
        .values(
            {
                "status": sql_constant("canceled"),
                "error": sql_constant("operator canceled work item"),
                "updated_at": bindparam("p0"),
            }
        )
        .where(
            and_(
                tables["vote_sessions"].c["workstream_id"].in_(bindparam("items", expanding=True)),
                not_(
                    tables["vote_sessions"]
                    .c["status"]
                    .in_(
                        [
                            sql_constant("published"),
                            sql_constant("superseded"),
                            sql_constant("canceled"),
                        ]
                    )
                ),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": value_1, "items": items})
