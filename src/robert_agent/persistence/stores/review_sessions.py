"""Shared stores operations for review_sessions."""

from robert_agent.foundation.contracts.execution import RoundStatus

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
    workstream_id,
    trigger,
    value_6,
    timestamp,
    timestamp_8,
):
    stmt = (
        insert(tables["review_sessions"])
        .values(
            {
                "round_id": bindparam("p0"),
                "repo": bindparam("p1"),
                "pr_number": bindparam("p2"),
                "workstream_id": bindparam("p3"),
                "trigger_fingerprint": bindparam("p4"),
                "request_json": bindparam("p5"),
                "created_at": bindparam("p6"),
                "updated_at": bindparam("p7"),
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
            "p3": workstream_id,
            "p4": trigger,
            "p5": value_6,
            "p6": timestamp,
            "p7": timestamp_8,
        },
    )


def set_status_and_updated_at_by_round_id(conn, status, now, round_id):
    stmt = (
        update(tables["review_sessions"])
        .values({"status": bindparam("p0"), "updated_at": bindparam("p1")})
        .where((tables["review_sessions"].c["round_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": status, "p1": now, "p2": round_id})


def cancel_execution_filtered(conn, items, value_1):
    stmt = (
        update(tables["review_sessions"])
        .values(
            {
                "status": sql_constant(RoundStatus.CANCELED.value),
                "error": sql_constant("operator canceled work item"),
                "updated_at": bindparam("p0"),
            }
        )
        .where(
            and_(
                tables["review_sessions"].c["workstream_id"].in_(bindparam("items", expanding=True)),
                not_(
                    tables["review_sessions"]
                    .c["status"]
                    .in_(
                        [
                            sql_constant(RoundStatus.PUBLISHED.value),
                            sql_constant(RoundStatus.SUPERSEDED.value),
                            sql_constant(RoundStatus.CANCELED.value),
                        ]
                    )
                ),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": value_1, "items": items})
