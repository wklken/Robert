"""Change only the exact, fenced source delivery recovery owner."""

import json

from sqlalchemy import insert, update

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables


def start(conn, *, pause, question_id, transition_id, binding_sha256, evidence, now):
    if not conn.in_transaction:
        raise ValueError("Delivery recovery requires a writer")
    table = tables["source_delivery_recoveries"]
    return (
        statements.write(
            conn,
            insert(table).values(
                source_result_id=pause["source_result_id"],
                source_id=pause["source_id"],
                generation=pause["generation"],
                question_id=question_id,
                transition_id=transition_id,
                binding_sha256=binding_sha256,
                evidence_json=json.dumps(evidence, sort_keys=True),
                claimed_at=now,
            ),
        )
        == 1
    )


def complete(conn, owner, *, now):
    if not conn.in_transaction:
        raise ValueError("Delivery completion requires a writer")
    table = tables["source_delivery_recoveries"]
    return (
        statements.write(
            conn,
            update(table)
            .where(
                table.c.source_result_id == owner["source_result_id"],
                table.c.generation == owner["generation"],
                table.c.transition_id == owner["transition_id"],
                table.c.completed_at.is_(None),
                table.c.suspended_at.is_(None),
            )
            .values(completed_at=now),
        )
        == 1
    )


def suspend(conn, owner, *, now):
    """End this delivery owner without claiming delivery or freeing unknown effects."""
    if not conn.in_transaction:
        raise ValueError("Delivery suspension requires a writer")
    table = tables["source_delivery_recoveries"]
    return (
        statements.write(
            conn,
            update(table)
            .where(
                table.c.source_result_id == owner["source_result_id"],
                table.c.generation == owner["generation"],
                table.c.transition_id == owner["transition_id"],
                table.c.completed_at.is_(None),
                table.c.suspended_at.is_(None),
            )
            .values(suspended_at=now),
        )
        == 1
    )
