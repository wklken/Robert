"""Shared stores operations for execution_pending_facts."""

from sqlalchemy import update, bindparam, and_, null
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def update_for_consume_fact(conn, now, value_2, source_signal_id):
    stmt = (
        update(tables["execution_pending_facts"])
        .values({"consumed_at": bindparam("p0"), "resolution_json": bindparam("p1")})
        .where(
            and_(
                (tables["execution_pending_facts"].c["source_signal_id"] == bindparam("p2")),
                tables["execution_pending_facts"].c["consumed_at"].is_(null()),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": value_2, "p2": source_signal_id})


def insert_for_apply(
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
):
    stmt = insert(tables["execution_pending_facts"]).values(
        {
            "source_signal_id": bindparam("p0"),
            "subject_type": bindparam("p1"),
            "subject_id": bindparam("p2"),
            "topic": bindparam("p3"),
            "dedupe_key": bindparam("p4"),
            "fact_id": bindparam("p5"),
            "disposition": bindparam("p6"),
            "repo_id": bindparam("p7"),
            "task_id": bindparam("p8"),
            "payload_json": bindparam("p9"),
            "not_before_at": bindparam("p10"),
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
        },
    )
