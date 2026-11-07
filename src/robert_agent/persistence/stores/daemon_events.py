"""Shared stores operations for daemon_events."""

from sqlalchemy import bindparam
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def record_event(conn, daemon_event_id, daemon_run_id, event_type, status, now, value_6):
    stmt = insert(tables["daemon_events"]).values(
        {
            "daemon_event_id": bindparam("p0"),
            "daemon_run_id": bindparam("p1"),
            "event_type": bindparam("p2"),
            "status": bindparam("p3"),
            "created_at": bindparam("p4"),
            "metadata_json": bindparam("p5"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": daemon_event_id,
            "p1": daemon_run_id,
            "p2": event_type,
            "p3": status,
            "p4": now,
            "p5": value_6,
        },
    )
