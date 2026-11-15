"""Read authoritative consumption and project legacy GitHub relationships."""

from sqlalchemy import func, select

from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries import source_members
from robert_agent.persistence.rounds.source_evidence import json_object


def task_relationship(binding):
    ledger = tables["request_events"]
    state = (
        select(ledger.c.state)
        .where(ledger.c.task_id == binding.c.task_id, ledger.c.event_id == binding.c.event_id)
        .correlate(binding)
        .scalar_subquery()
    )
    return func.coalesce(state, binding.c.relationship)


def for_ingress_request(conn, request_id):
    event = tables["request_events"]
    return statements.read(
        conn, select(event).where(event.c.request_id == request_id).order_by(event.c.created_at, event.c.event_id)
    )


def source_round_events(conn, round_id):
    """Project each exact frozen trigger from the sole event ledger."""
    result = []
    for member in source_members.frozen_members(conn, round_id):
        request = member.ownership.request
        snapshot = json_object(member.member["content_snapshot_json"])
        for event in member.ownership.events:
            ledger = {key: event[key] for key in tables["request_events"].c.keys()}
            row = {
                **ledger,
                "event_fingerprint": event["event_fingerprint"],
                "content_version": member.member["content_version"],
                "auth_type": request["auth_type"],
                "authorization_json": request["authorization_json"],
                "phase": request["phase"],
                "repo_id": request["repo_id"],
                "frozen_event_id": snapshot.get("event_id"),
            }
            result.append(records.Record(tuple(row), tuple(row.values())))
    return result


def frozen_model_members(conn, round_id):
    """Return exact frozen request membership; event fingerprints remain independent of IDs."""
    grouped = {}
    for row in source_round_events(conn, round_id):
        group = grouped.setdefault(
            row["request_id"],
            {
                "request_id": row["request_id"],
                "content_version": row["content_version"],
                "events": [],
            },
        )
        group["events"].append(
            {
                "row_id": row["row_id"],
                "event_id": row["event_id"],
                "fingerprint": row["event_fingerprint"],
            }
        )
    member = tables["source_round_requests"]
    count = statements.read(conn, select(func.count()).select_from(member).where(member.c.round_id == round_id))[0][0]
    return list(grouped.values()) if len(grouped) == count else []
