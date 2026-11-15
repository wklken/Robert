"""Persist one immutable native GitHub request and its independently proved effect."""

import json

from sqlalchemy import insert, update

from robert_agent.foundation.contracts.delivery import PublicationStatus, PublishStatus
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries import source_github_publication as read
from robert_agent.persistence.rounds.source_evidence import context_digest


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def insert_preparation(conn, *, source_result_id, action_id, publication_id, packet):
    row = read.required_action(conn, source_result_id, action_id)
    if (
        not conn.in_transaction
        or row is None
        or row["publication_id"] != publication_id
        or row["intent_status"] != PublicationStatus.PREPARED
        or row["prepared_evidence_json"] is not None
        or packet["identity"] != read.request_identity(row)
        or context_digest(packet) != row["request_fingerprint"]
    ):
        raise ValueError("Native GitHub preparation does not match the exact intent")
    table = tables["github_publication_preparations"]
    statements.write(conn, insert(table).values(publication_id=publication_id, prepared_evidence_json=_encode(packet)))


def confirm_effect(conn, *, source_result_id, action_id, publication_id, fingerprint, proof, observed_at, now):
    row = read.required_action(conn, source_result_id, action_id)
    if (
        not conn.in_transaction
        or row is None
        or row["publication_id"] != publication_id
        or row["request_fingerprint"] != fingerprint
        or row["intent_status"] not in (PublicationStatus.EXECUTING, PublicationStatus.ATTENTION)
        or row["publish_status"] != PublishStatus.NOT_PUBLISHED
        or row["external_id"] is not None
    ):
        return None
    packet = read.prepared_request(row)
    accepted, started, observed, ended = map(
        utc_timestamp, (row["accepted_at"], row["intent_started_at"], observed_at, now)
    )
    if packet is None or None in (accepted, started, observed, ended) or not accepted <= started <= observed <= ended:
        return None
    if not read.canonical_remote_proof(packet, proof):
        return None
    receipt = {
        "status": "confirmed_effect",
        "receipt": {
            "identity": packet["identity"],
            "publication_id": publication_id,
            "request_fingerprint": fingerprint,
            "proof": proof,
            "observed_at": observed_at,
        },
    }
    intents, actions = (tables[name] for name in ("publication_intents", "github_actions"))
    pr = proof.get("pr")
    effect = packet["effect"]
    external_id = str(pr["number"]) if row["action_type"] == "open_pr" else effect["branch"]
    if (
        statements.write(
            conn,
            update(intents)
            .where(
                intents.c.publication_id == publication_id,
                intents.c.action_id == action_id,
                intents.c.status == row["intent_status"],
                intents.c.request_fingerprint == fingerprint,
                intents.c.started_at == row["intent_started_at"],
            )
            .values(
                status=PublicationStatus.PUBLISHED.value,
                finished_at=now,
                updated_at=now,
                remote_evidence_json=_encode(receipt),
                error_json=None,
            ),
        )
        != 1
    ):
        return None
    if (
        statements.write(
            conn,
            update(actions)
            .where(
                actions.c.action_id == action_id,
                actions.c.provider == "github",
                actions.c.publish_status == PublishStatus.NOT_PUBLISHED.value,
                actions.c.external_id.is_(None),
            )
            .values(
                publish_status=PublishStatus.PUBLISHED.value,
                external_id=external_id,
                target_url=pr["url"] if pr else row["target_url"],
            ),
        )
        != 1
    ):
        raise ValueError("Native GitHub action changed while recording its receipt")
    confirmed = read.confirmed_effect(conn, source_result_id, action_id)
    if confirmed is None:
        raise ValueError("Native GitHub receipt did not revalidate")
    return confirmed
