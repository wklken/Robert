"""application delivery receipts responsibilities."""

import re
from robert_agent.workflows.review import publish as review_publish
from robert_agent.workflows.vote import publish as vote_publish
from robert_agent.adapters.sqlite import execution_control as execution_store

from robert_agent.delivery.application import effects as publication_effects
from robert_agent.delivery.application import scope as publication_scope
from robert_agent.delivery import notifications as publication_notifications
from robert_agent.delivery import values as publication_values

COMMENT_MARKER_RE = re.compile(
    r"<!--\s*robert-comment\s+task_id=\S+\s+attempt_id=\S+\s+action_index=\d+\s*-->",
    re.I | re.S,
)

WORKSTREAM_MARKER_RE = re.compile(
    r"<!--\s*robert-workstream\b.*?-->",
    re.I | re.S,
)


def delivery_receipt(conn, action, result, now):
    """Bind the publisher's confirmation to the audited action, without inventing evidence."""
    scope = publication_scope.trusted_action_scope(conn, action) or {}
    response = result.get("response") or {}
    reconciliation = response.get("reconciliation") or {}
    metadata = publication_values.decode_json(action.get("metadata_json"))
    marker = publication_effects.hidden_marker(
        metadata.get("body", ""),
        WORKSTREAM_MARKER_RE if action["action_type"] == "open_pr" else COMMENT_MARKER_RE,
    )
    return {
        "schema_version": 1,
        "action_id": action["action_id"],
        "result_id": action.get("result_id"),
        "task_id": action.get("task_id"),
        "action_type": action["action_type"],
        "scope": scope,
        "external_id": result.get("external_id") or reconciliation.get("external_id"),
        "target_url": result.get("target_url") or reconciliation.get("target_url") or action.get("target_url"),
        "result_head_sha": (
            result.get("result_head_sha")
            or reconciliation.get("source_sha")
            or (reconciliation.get("remote_sha") if action["action_type"] == "push_existing_pr" else None)
            or response.get("head_sha")
        ),
        "expected_marker": marker,
        "confirmation_method": (
            "remote_reconciliation"
            if reconciliation
            else "remote_deduplication"
            if result.get("deduplicated")
            else "publisher_acknowledgement"
        ),
        "confirmed_at": now,
    }


def mark_published(conn, action, result, now, intent=None):
    review_publish.record_outcome(conn, action, "published", now)
    vote_publish.record_outcome(conn, action, "published", now)
    metadata = publication_values.decode_json(action["metadata_json"])
    receipt = delivery_receipt(conn, action, result, now)
    metadata["publish"] = {
        "receipt": receipt,
        "status": "published",
        "published_at": now,
        "response": result.get("response", {}),
        "deduplicated": bool(result.get("deduplicated")),
    }
    conn.execute(
        """
        UPDATE github_actions
        SET publish_status = 'published',
            external_id = COALESCE(NULLIF(?, ''), external_id),
            target_url = COALESCE(NULLIF(?, ''), target_url),
            metadata_json = ?
        WHERE action_id = ?
        """,
        (
            result.get("external_id", ""),
            result.get("target_url"),
            publication_values.encode_json(metadata),
            action["action_id"],
        ),
    )
    publication_notifications.resolve_action_notification(conn, action, "github_publish_failed", result, now)
    if intent:
        current = intent["status"]
        if current in {"executing", "failed"}:
            if current == "failed":
                execution_store.transition_publication_intent(conn, intent["publication_id"], "executing", now)
            execution_store.transition_publication_intent(
                conn,
                intent["publication_id"],
                "published",
                now,
                remote_evidence={
                    **publication_values.decode_json(intent.get("remote_evidence_json")),
                    "status": "confirmed_effect",
                    "reason_code": "publisher_success",
                    "reconciliation": (result.get("response") or {}).get("reconciliation", {}),
                    "receipt": receipt,
                },
            )
