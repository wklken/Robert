"""Apply local retirement only to the exact accepted root's never-started effects."""

import json

from sqlalchemy import func, insert, select, update

from robert_agent.foundation.contracts.delivery import PublicationStatus, PublishStatus
from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.expressions import valid_json_or_empty
from robert_agent.persistence.queries.source_retirement import intent_disposition


def retire(conn, root, effects, *, proof, revision, now):
    if not conn.in_transaction:
        raise ValueError("Accepted retirement requires the caller's writer")
    action, intent, link, fact = (
        tables[name]
        for name in ("github_actions", "publication_intents", "source_required_actions", "source_result_retirements")
    )
    root_id = root["source_result_id"]
    for row in effects:
        action_id = row["action_id"]
        if (
            row["publication_id"] is not None
            and statements.write(
                conn,
                update(intent)
                .where(
                    intent.c.publication_id == row["publication_id"],
                    intent.c.action_id == action_id,
                    intent.c.status == PublicationStatus.PREPARED,
                    intent.c.started_at.is_(None),
                    intent.c.finished_at.is_(None),
                    intent.c.request_fingerprint == row["intent_fingerprint"],
                )
                .values(
                    status=PublicationStatus.FAILED,
                    finished_at=now,
                    updated_at=now,
                    error_json=json.dumps(intent_disposition(root_id, action_id), sort_keys=True),
                ),
            )
            != 1
        ):
            return False
        if (
            statements.write(
                conn,
                update(action)
                .where(
                    action.c.action_id == action_id,
                    action.c.publish_status == PublishStatus.NOT_PUBLISHED,
                    select(1).where(link.c.action_id == action_id, link.c.source_result_id == root_id).exists(),
                )
                .values(publish_status=PublishStatus.SKIPPED),
            )
            != 1
        ):
            return False
    return (
        statements.write(
            conn,
            insert(fact).values(
                source_result_id=root_id,
                observation_revision=revision,
                proof_json=json.dumps(proof, sort_keys=True),
                retired_at=now,
            ),
        )
        == 1
    )


def close_canceled_round(conn, root, *, now):
    round_, cancellation, retired = (
        tables[name] for name in ("source_rounds", "source_round_cancellations", "source_result_retirements")
    )
    return (
        statements.write(
            conn,
            update(round_)
            .where(
                round_.c.round_id == root["round_id"],
                round_.c.source_id == root["source_id"],
                round_.c.status == "active",
                select(1).where(cancellation.c.round_id == root["round_id"]).exists(),
                select(1).where(retired.c.source_result_id == root["source_result_id"]).exists(),
            )
            .values(status="canceled", finished_at=now),
        )
        == 1
    )


def touch_projection(conn, *, source_id, repo_id, round_id, item_id, now):
    item = tables["work_items"]
    return (
        statements.write(
            conn,
            update(item)
            .where(
                item.c.work_item_id == item_id,
                item.c.origin_type == "tapd",
                item.c.origin_source_id == source_id,
                item.c.repo_id == repo_id,
                item.c.canceled_at.is_(None),
                item.c.completed_at.is_(None),
                func.json_extract(valid_json_or_empty(item.c.metadata_json), "$.native_projection_round_id")
                == round_id,
            )
            .values(version=item.c.version + 1, updated_at=now),
        )
        == 1
    )
