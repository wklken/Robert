"""Shared stores operations for publication_intents."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import bindparam
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_for_prepare_publication_intent(
    conn, publication_id, action_id, idempotency_key, request_fingerprint, now, now_6
):
    stmt = insert(tables["publication_intents"]).values(
        {
            "publication_id": bindparam("p0"),
            "action_id": bindparam("p1"),
            "idempotency_key": bindparam("p2"),
            "request_fingerprint": bindparam("p3"),
            "status": sql_constant("prepared"),
            "created_at": bindparam("p4"),
            "updated_at": bindparam("p5"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": publication_id,
            "p1": action_id,
            "p2": idempotency_key,
            "p3": request_fingerprint,
            "p4": now,
            "p5": now_6,
        },
    )
