"""Idempotent creation and updates for proposal notifications."""

from sqlalchemy import func, update
from sqlalchemy.dialects.sqlite import insert

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries.proposal_feedback import CHANNEL


def record(conn, identifier, kind, now, metadata_json, *, operator=False):
    notice = tables["notifications"]
    statement = insert(notice).values(
        notification_id=identifier,
        notification_type=kind,
        channel="local" if operator else CHANNEL,
        status="recorded" if operator else "pending",
        created_at=now,
        metadata_json=metadata_json,
    )
    return statements.write(conn, statement.on_conflict_do_nothing(index_elements=[notice.c.notification_id]))


def update_status(conn, identifier, status, delivery_json):
    notice = tables["notifications"]
    return statements.write(
        conn,
        update(notice)
        .where(notice.c.notification_id == identifier, notice.c.channel == CHANNEL, notice.c.status == "pending")
        .values(
            status=status, metadata_json=func.json_set(notice.c.metadata_json, "$.delivery", func.json(delivery_json))
        ),
    )
