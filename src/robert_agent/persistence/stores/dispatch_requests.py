"""Conditional dispatch writes participating in caller-owned transactions."""

from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert

from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def create(conn, values):
    return statements.write(conn, insert(tables["dispatch_requests"]).values(values))


def transition(conn, request_id, expected, status, now, **fields):
    table = tables["dispatch_requests"]
    return statements.write(
        conn,
        update(table)
        .where(table.c.request_id == request_id, table.c.status == expected)
        .values(status=status, updated_at=now, **fields),
    )


def notification(conn, request_id, kind, now, metadata, task_id=None):
    return statements.write(
        conn,
        insert(tables["notifications"])
        .values(
            notification_id=f"dispatch:{request_id}:{kind}",
            task_id=task_id,
            notification_type=f"dispatch_{kind}",
            channel="local",
            status="recorded",
            created_at=now,
            metadata_json=metadata,
        )
        .on_conflict_do_nothing(index_elements=["notification_id"]),
    )
