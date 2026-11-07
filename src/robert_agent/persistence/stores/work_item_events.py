"""Shared stores operations for work_item_events."""

from sqlalchemy import bindparam
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_event(
    conn,
    event_id,
    work_item_id,
    event_type,
    actor_kind,
    actor_identity,
    body,
    resolves_event_id,
    idempotency_key,
    now,
    metadata_json,
):
    stmt = insert(tables["work_item_events"]).values(
        {
            "event_id": bindparam("p0"),
            "work_item_id": bindparam("p1"),
            "event_type": bindparam("p2"),
            "actor_kind": bindparam("p3"),
            "actor_identity": bindparam("p4"),
            "body": bindparam("p5"),
            "resolves_event_id": bindparam("p6"),
            "idempotency_key": bindparam("p7"),
            "created_at": bindparam("p8"),
            "metadata_json": bindparam("p9"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": event_id,
            "p1": work_item_id,
            "p2": event_type,
            "p3": actor_kind,
            "p4": actor_identity,
            "p5": body,
            "p6": resolves_event_id,
            "p7": idempotency_key,
            "p8": now,
            "p9": metadata_json,
        },
    )
