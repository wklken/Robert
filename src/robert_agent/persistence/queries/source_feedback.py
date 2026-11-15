"""Exact prepared request and independent receipt for root-owned GitHub feedback."""

import re
from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import select

from robert_agent.foundation.contracts.delivery import PublicationStatus, PublishStatus, SourceControllerAction
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries.source_pr_lineage import confirmed_pr_lineage
from robert_agent.persistence.rounds.source_evidence import context_digest, json_object
from robert_agent.persistence.rounds.source_feedback import action_matches, fixed_body, planned_actions


@dataclass(frozen=True, slots=True)
class FeedbackReceipt:
    publication_id: str
    request_fingerprint: str
    external_id: str
    url: str
    observed_at: str


def required_action(conn, root_id, action_id):
    root, link, action, intent, preparation, worker = (
        tables[name]
        for name in (
            "source_results",
            "source_required_actions",
            "github_actions",
            "publication_intents",
            "github_publication_preparations",
            "worker_results",
        )
    )
    return records.first(
        statements.read(
            conn,
            select(
                action,
                root.c.source_id,
                root.c.accepted_at,
                root.c.kind,
                root.c.evidence_json,
                root.c.round_id,
                root.c.input_version,
                root.c.worker_result_id,
                root.c.model_run_id,
                worker.c.metadata_json.label("candidate_metadata_json"),
                link.c.position,
                intent.c.publication_id,
                intent.c.status.label("intent_status"),
                intent.c.request_fingerprint,
                intent.c.started_at,
                intent.c.finished_at,
                intent.c.remote_evidence_json,
                preparation.c.prepared_evidence_json,
            )
            .select_from(
                link.join(root, root.c.source_result_id == link.c.source_result_id)
                .join(action, action.c.action_id == link.c.action_id)
                .outerjoin(intent, intent.c.action_id == action.c.action_id)
                .outerjoin(preparation, preparation.c.publication_id == intent.c.publication_id)
                .outerjoin(worker, worker.c.result_id == root.c.worker_result_id)
            )
            .where(
                link.c.source_result_id == root_id,
                action.c.action_id == action_id,
                action.c.source_result_id == root_id,
                action.c.provider == "github",
                action.c.action_type == SourceControllerAction.GITHUB_FEEDBACK.value,
                action.c.task_id.is_(None),
                action.c.result_id.is_(None),
            ),
        )
    )


def planned_action(row):
    evidence = json_object(row["evidence_json"]) or {}
    planned = planned_actions(
        row["source_result_id"],
        evidence.get("github_return_targets"),
        evidence.get("github_feedback_code_sha256"),
    )
    return next((item for item in planned or () if action_matches(row, row, item)), None)


def lineage(conn, row, action):
    target = action.target
    value = confirmed_pr_lineage(
        conn,
        source_id=row["source_id"],
        repo_id=target["repo_id"],
        workstream_id=target["pr_workstream_id"],
        action_id=target["creation_action_id"],
    )
    if value is None or (
        value.origin_workstream_id,
        value.pr_source_id,
        value.source_result_id,
        value.publication_id,
        value.request_fingerprint,
        value.repository,
        value.number,
        value.url,
    ) != (
        target["origin_workstream_id"],
        target["pr_source_id"],
        target["creation_result_id"],
        target["creation_publication_id"],
        target["creation_request_fingerprint"],
        target["repository"],
        target["pr_number"],
        target["pr_url"],
    ):
        return None
    return value


def code_projection(row):
    metadata = json_object(row["candidate_metadata_json"]) or {}
    audit = metadata.get("audit")
    scope = audit.get("action_scope") if isinstance(audit, dict) else None
    value = scope.get("native_github") if isinstance(scope, dict) else None
    return value if isinstance(value, dict) else None


def packet_for(conn, row, publisher):
    action = planned_action(row)
    if action is None or not isinstance(publisher, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,38}", publisher):
        return None
    original = lineage(conn, row, action)
    if original is None:
        return None
    recipe = action.metadata["feedback"]
    marker = recipe["marker"]
    body = fixed_body(action)
    if recipe["mode"] == "code_checks":
        projection = code_projection(row)
        if (
            not projection
            or context_digest(projection) != recipe["code_sha256"]
            or not isinstance(projection.get("body"), str)
        ):
            return None
        body = projection["body"] + "\n\n<!-- " + marker + " -->"
    if body is None or body.count(marker) != 1 or len(body.encode()) > 65536:
        return None
    return {
        "version": 1,
        "root_id": row["source_result_id"],
        "source_id": row["source_id"],
        "round_id": row["round_id"],
        "input_version": row["input_version"],
        "accepted_at": row["accepted_at"],
        "candidate_id": row["worker_result_id"] or row["model_run_id"],
        "action_id": row["action_id"],
        "position": row["position"],
        "target": action.target,
        "recipe": recipe,
        "body": body,
        "body_sha256": sha256(body.encode()).hexdigest(),
        "publisher": publisher,
    }


def prepared_request(conn, row):
    packet = json_object(row["prepared_evidence_json"])
    if (
        packet is None
        or row["request_fingerprint"] != context_digest(packet)
        or packet_for(conn, row, packet.get("publisher")) != packet
    ):
        return None
    return packet


def comment_url(target, external_id):
    return f"{target['pr_url']}#issuecomment-{external_id}"


def canonical_proof(packet, row, proof, observed_at, finished_at):
    if not isinstance(proof, dict) or set(proof) != {
        "external_id",
        "url",
        "creator",
        "body_sha256",
        "created_at",
    }:
        return False
    external_id = proof["external_id"]
    if not isinstance(external_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", external_id):
        return False
    accepted, started, observed, finished, created = map(
        utc_timestamp,
        (row["accepted_at"], row["started_at"], observed_at, finished_at, proof["created_at"]),
    )
    return (
        None not in (accepted, started, observed, finished, created)
        and accepted <= started <= observed <= finished
        and started.replace(microsecond=0) <= created <= observed
        and proof["url"] == comment_url(packet["target"], external_id)
        and proof["creator"] == packet["publisher"]
        and proof["body_sha256"] == packet["body_sha256"]
    )


def confirmed_receipt(conn, root_id, action_id):
    row = required_action(conn, root_id, action_id)
    if (
        row is None
        or row["publish_status"] != PublishStatus.PUBLISHED
        or row["intent_status"] != PublicationStatus.PUBLISHED
    ):
        return None
    packet = prepared_request(conn, row)
    receipt = json_object(row["remote_evidence_json"]) or {}
    if (
        packet is None
        or receipt.get("status") != "confirmed_feedback"
        or set(receipt)
        != {
            "status",
            "publication_id",
            "request_fingerprint",
            "proof",
            "observed_at",
        }
    ):
        return None
    proof, observed = receipt["proof"], receipt["observed_at"]
    if (
        receipt["publication_id"] != row["publication_id"]
        or receipt["request_fingerprint"] != row["request_fingerprint"]
        or not canonical_proof(packet, row, proof, observed, row["finished_at"])
        or row["external_id"] != proof["external_id"]
        or row["target_url"] != proof["url"]
    ):
        return None
    return FeedbackReceipt(
        row["publication_id"], row["request_fingerprint"], proof["external_id"], proof["url"], observed
    )
