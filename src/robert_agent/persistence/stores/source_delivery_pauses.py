"""Append the immutable proof of an accepted dormant delivery."""

import json

from sqlalchemy import insert

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries import source_delivery_pauses as read


def record(conn, *, root, revision, proof, now):
    if not conn.in_transaction:
        raise ValueError("Delivery pause requires a caller-owned writer")
    prior = read.latest(conn, root["source_result_id"])
    return (
        statements.write(
            conn,
            insert(tables["source_delivery_pauses"]).values(
                source_id=root["source_id"],
                source_result_id=root["source_result_id"],
                round_id=root["round_id"],
                generation=1 if prior is None else prior["generation"] + 1,
                observation_revision=revision,
                proof_json=json.dumps(proof, sort_keys=True),
                paused_at=now,
            ),
        )
        == 1
    )
