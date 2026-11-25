"""application delivery receipts responsibilities."""

from robert_agent.foundation.contracts.delivery import GitHubActionType, PublicationStatus, PublishStatus

from robert_agent.persistence.stores import github_actions as db_write_github_actions
from robert_agent.persistence.queries import github_actions as action_queries
import re
from robert_agent.workflows.review import publish as review_publish
from robert_agent.workflows.vote import publish as vote_publish
from robert_agent.persistence import execution_control as execution_store

from robert_agent.delivery.application import effects as publication_effects
from robert_agent.delivery.application import scope as publication_scope
from robert_agent.delivery.application.reconcile import derive_publication_keys
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
        WORKSTREAM_MARKER_RE if action["action_type"] == GitHubActionType.OPEN_PR else COMMENT_MARKER_RE,
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
            or (
                reconciliation.get("remote_sha") if action["action_type"] == GitHubActionType.PUSH_EXISTING_PR else None
            )
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
    review_publish.record_outcome(conn, action, PublishStatus.PUBLISHED.value, now)
    vote_publish.record_outcome(conn, action, PublishStatus.PUBLISHED.value, now)
    metadata = publication_values.decode_json(action["metadata_json"])
    receipt = delivery_receipt(conn, action, result, now)
    metadata["publish"] = {
        "receipt": receipt,
        "status": PublishStatus.PUBLISHED.value,
        "published_at": now,
        "response": result.get("response", {}),
        "deduplicated": bool(result.get("deduplicated")),
    }
    db_write_github_actions.mark_published(
        conn,
        result.get("external_id", ""),
        result.get("target_url"),
        publication_values.encode_json(metadata),
        action["action_id"],
    )
    publication_notifications.resolve_action_notification(conn, action, "github_publish_failed", result, now)
    if intent:
        current = intent["status"]
        if current in {PublicationStatus.EXECUTING.value, PublicationStatus.FAILED.value}:
            if current == PublicationStatus.FAILED.value:
                execution_store.transition_publication_intent(
                    conn, intent["publication_id"], PublicationStatus.EXECUTING.value, now
                )
            execution_store.transition_publication_intent(
                conn,
                intent["publication_id"],
                PublicationStatus.PUBLISHED.value,
                now,
                remote_evidence={
                    **publication_values.decode_json(intent.get("remote_evidence_json")),
                    "status": "confirmed_effect",
                    "reason_code": "publisher_success",
                    "reconciliation": (result.get("response") or {}).get("reconciliation", {}),
                    "receipt": receipt,
                },
            )


def adopt_review_publication(conn, result_id, scope, now):
    """Attach confirmed PR-first effects to the audited actions in the audit transaction."""
    publication = scope.get("pre_pr_review", {}).get("publication")
    if not publication:
        return
    initial, pushed = publication["initial"], publication["push"]
    head, number = scope["result_head_sha"], initial["number"]
    if (
        type(number) is not int
        or number <= 0
        or initial["repo"] != scope["repo"]
        or initial["workstream_id"] != scope["workstream_id"]
        or initial["branch"] != scope["branch_name"]
        or initial["url"] != f"https://github.com/{scope['repo']}/pull/{number}"
        or pushed["branch"] != initial["branch"]
        or pushed["head_sha"] != head
        or publication["head_sha"] != head
    ):
        raise ValueError("confirmed review publication differs from its audited target")

    for row in action_queries.find_for_publication_facts(conn, now, result_id, None):
        kind = row["action_type"]
        if kind not in {GitHubActionType.OPEN_PR, GitHubActionType.PUSH_EXISTING_PR}:
            continue
        opened = kind == GitHubActionType.OPEN_PR
        action = {**dict(row), "result_id": result_id}
        key, fingerprint = derive_publication_keys(action, scope)
        intent = execution_store.prepare_publication_intent(conn, action["action_id"], key, fingerprint, now)
        intent = execution_store.transition_publication_intent(
            conn, intent["publication_id"], PublicationStatus.EXECUTING.value, now
        )
        evidence = initial if opened else pushed
        mark_published(
            conn,
            action,
            {
                "external_id": str(number) if opened else pushed["branch"],
                "target_url": initial["url"] if opened else action["target_url"],
                "result_head_sha": head,
                "deduplicated": True,
                "response": {
                    "reconciliation": {
                        "source_sha": head,
                        "round_id": scope["pre_pr_review"]["round_id"],
                        "observed_at": evidence["confirmed_at"],
                    }
                },
            },
            now,
            intent=intent,
        )
