"""Write immutable preparation and canonical receipt for one feedback intent."""

import json

from sqlalchemy import insert, update

from robert_agent.foundation.contracts.delivery import PublicationStatus, PublishStatus, SourceControllerAction
from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries import source_feedback as read
from robert_agent.persistence.rounds.source_evidence import context_digest


def insert_preparation(conn, row, publication_id, packet):
    if (
        not conn.in_transaction
        or row["publication_id"] != publication_id
        or row["intent_status"] != PublicationStatus.PREPARED
        or row["prepared_evidence_json"] is not None
        or row["request_fingerprint"] != context_digest(packet)
        or read.packet_for(conn, row, packet.get("publisher")) != packet
    ):
        raise ValueError("Feedback preparation changed")
    statements.write(
        conn,
        insert(tables["github_publication_preparations"]).values(
            publication_id=publication_id,
            prepared_evidence_json=json.dumps(packet, ensure_ascii=False, sort_keys=True),
        ),
    )


def confirm(conn, row, proof, observed_at, now):
    if (
        not conn.in_transaction
        or row["intent_status"] not in (PublicationStatus.EXECUTING, PublicationStatus.ATTENTION)
        or row["publish_status"] != PublishStatus.NOT_PUBLISHED
        or row["external_id"] is not None
        or row["target_url"] is not None
    ):
        return None
    packet = read.prepared_request(conn, row)
    if packet is None or not read.canonical_proof(packet, row, proof, observed_at, now):
        return None
    action_id, publication_id = row["action_id"], row["publication_id"]
    intent, action = (tables[name] for name in ("publication_intents", "github_actions"))
    evidence = {
        "status": "confirmed_feedback",
        "publication_id": publication_id,
        "request_fingerprint": row["request_fingerprint"],
        "proof": proof,
        "observed_at": observed_at,
    }
    if (
        statements.write(
            conn,
            update(intent)
            .where(
                intent.c.publication_id == publication_id,
                intent.c.action_id == action_id,
                intent.c.status == row["intent_status"],
                intent.c.request_fingerprint == row["request_fingerprint"],
                intent.c.started_at == row["started_at"],
            )
            .values(
                status=PublicationStatus.PUBLISHED.value,
                remote_evidence_json=json.dumps(evidence, sort_keys=True),
                error_json=None,
                finished_at=now,
                updated_at=now,
            ),
        )
        != 1
    ):
        return None
    if (
        statements.write(
            conn,
            update(action)
            .where(
                action.c.action_id == action_id,
                action.c.source_result_id == row["source_result_id"],
                action.c.provider == "github",
                action.c.action_type == SourceControllerAction.GITHUB_FEEDBACK.value,
                action.c.publish_status == PublishStatus.NOT_PUBLISHED.value,
                action.c.external_id.is_(None),
            )
            .values(
                publish_status=PublishStatus.PUBLISHED.value,
                external_id=proof["external_id"],
                target_url=proof["url"],
            ),
        )
        != 1
    ):
        raise ValueError("Feedback action changed during receipt recording")
    receipt = read.confirmed_receipt(conn, row["source_result_id"], action_id)
    if receipt is None:
        raise ValueError("Feedback receipt did not revalidate")
    return receipt
