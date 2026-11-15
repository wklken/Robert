"""Record a read-confirmed native TAPD effect under the caller's source fence."""

import json

from sqlalchemy import and_, func, or_, select, update

from robert_agent.foundation.contracts.delivery import AuditStatus, PublicationStatus, PublishStatus
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.expressions import valid_json_or_empty
from robert_agent.persistence.queries import source_results


def confirm_tapd_comment(conn, *, source_id, source_result_id, action_id, publication_id, receipt, now):
    """Atomically finalize only a matching started TAPD effect in the caller's writer."""
    if not conn.in_transaction:
        raise ValueError("Source publication receipt requires a caller-owned transaction")
    root = source_results.for_source(conn, source_id, source_result_id)
    source = source_results.tapd_source(conn, source_id)
    if root is None or source is None:
        return False
    action = next(
        (row for row in source_results.required_actions(conn, source_result_id) if row["action_id"] == action_id),
        None,
    )
    if (
        action is None
        or action["publication_id"] != publication_id
        or action["intent_status"] not in (PublicationStatus.EXECUTING, PublicationStatus.ATTENTION)
        or utc_timestamp(action["intent_started_at"]) is None
        or action["provider"] != "tapd"
        or action["action_type"] != "comment"
        or action["audit_status"] != AuditStatus.ACCEPTED
        or action["publish_status"] != PublishStatus.NOT_PUBLISHED
        or action["external_id"] is not None
    ):
        return False
    if not isinstance(receipt, dict):
        return False
    own_body_evidence = receipt.get("own_body_evidence_id")
    confirmed_at, started_at, observed_now = map(
        utc_timestamp, (receipt.get("confirmed_at"), action["intent_started_at"], now)
    )
    if (
        not isinstance(own_body_evidence, str)
        or not own_body_evidence.strip()
        or confirmed_at is None
        or observed_now is None
        or not started_at <= confirmed_at <= observed_now
    ):
        return False
    remote = {"status": "confirmed_effect", "receipt": receipt}
    remote_json = json.dumps(remote, ensure_ascii=False, allow_nan=False, sort_keys=True)
    target = source_results.source_target(source)
    confirmed = {
        **dict(action),
        "intent_status": PublicationStatus.PUBLISHED,
        "publish_status": PublishStatus.PUBLISHED,
        "external_id": receipt.get("external_id"),
        "remote_evidence_json": remote_json,
    }
    if source_results.confirmed_comment(confirmed, target) is None:
        return False
    intents, actions, links, prepared = (
        tables[name]
        for name in (
            "publication_intents",
            "github_actions",
            "source_required_actions",
            "tapd_publication_preparations",
        )
    )
    # The generic intent graph keeps ATTENTION terminal. Only an independently
    # confirmed TAPD receipt can settle this already-started action.
    if (
        statements.write(
            conn,
            update(intents)
            .where(
                intents.c.publication_id == publication_id,
                intents.c.action_id == action_id,
                intents.c.status == action["intent_status"],
                intents.c.started_at == action["intent_started_at"],
                select(links.c.action_id)
                .where(links.c.source_result_id == source_result_id, links.c.action_id == action_id)
                .exists(),
                select(prepared.c.publication_id)
                .where(
                    prepared.c.publication_id == publication_id,
                    prepared.c.prepared_evidence_json == action["prepared_evidence_json"],
                )
                .exists(),
            )
            .values(
                status=PublicationStatus.PUBLISHED.value,
                finished_at=now,
                updated_at=now,
                remote_evidence_json=remote_json,
                error_json=None,
            ),
        )
        != 1
    ):
        return False
    if (
        statements.write(
            conn,
            update(actions)
            .where(
                actions.c.action_id == action_id,
                actions.c.provider == "tapd",
                actions.c.action_type == "comment",
                actions.c.audit_status == AuditStatus.ACCEPTED.value,
                actions.c.publish_status == PublishStatus.NOT_PUBLISHED.value,
                actions.c.external_id.is_(None),
                actions.c.target_json == action["target_json"],
            )
            .values(publish_status=PublishStatus.PUBLISHED.value, external_id=receipt["external_id"]),
        )
        != 1
    ):
        raise ValueError("TAPD action changed while recording its receipt")
    return True


def save_action_pacing(conn, *, source_id, source_result_id, action_id, fields, provider="tapd"):
    """Store scheduling metadata under the caller's current source fence."""
    action, link, root, cancel, intent = (
        tables[name]
        for name in (
            "github_actions",
            "source_required_actions",
            "source_results",
            "source_round_cancellations",
            "publication_intents",
        )
    )
    unsettled = (
        select(link.c.action_id)
        .select_from(
            link.join(root, root.c.source_result_id == link.c.source_result_id)
            .outerjoin(cancel, cancel.c.round_id == root.c.round_id)
            .outerjoin(intent, intent.c.action_id == link.c.action_id)
        )
        .where(
            link.c.action_id == action.c.action_id,
            root.c.source_id == source_id,
            root.c.source_result_id == source_result_id,
            or_(
                and_(
                    cancel.c.round_id.is_(None),
                    or_(intent.c.status.is_(None), intent.c.status == PublicationStatus.PREPARED.value),
                ),
                intent.c.status.in_((PublicationStatus.EXECUTING.value, PublicationStatus.ATTENTION.value)),
            ),
        )
        .correlate(action)
        .exists()
    )
    patch = json.dumps({"publish": fields}, ensure_ascii=False, allow_nan=False, sort_keys=True)
    return bool(
        statements.write(
            conn,
            update(action)
            .where(
                action.c.action_id == action_id,
                action.c.provider == provider,
                action.c.audit_status == AuditStatus.ACCEPTED.value,
                action.c.publish_status == PublishStatus.NOT_PUBLISHED.value,
                unsettled,
            )
            .values(metadata_json=func.json_patch(valid_json_or_empty(action.c.metadata_json), patch)),
        )
    )
