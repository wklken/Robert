"""Atomic admission of a source request and its initial event membership."""

import json

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from robert_agent.persistence import statements, transactions
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries import ingress_requests, request_events
from robert_agent.persistence.stores.request_events import enqueue_ingress_events


def enqueue(
    conn,
    *,
    request_id,
    source_id,
    provider,
    auth_type,
    authorization,
    content_snapshot,
    content_version,
    event_ids,
    now,
    repo_id=None,
):
    """Admit one source observation, preserving an exact retry's initial state."""
    if any(
        not isinstance(value, str) or not value.strip()
        for value in (request_id, source_id, provider, auth_type, content_version)
    ):
        raise ValueError("Ingress request identity, authority type and version must be non-empty strings")
    identifiers = tuple(dict.fromkeys(event_ids))
    if not identifiers or any(not isinstance(value, str) or not value.strip() for value in identifiers):
        raise ValueError("An ingress request requires non-empty source event IDs")
    source, event, request = (tables[name] for name in ("sources", "source_events", "ingress_requests"))
    values = {
        "request_id": request_id,
        "source_id": source_id,
        "provider": provider,
        "auth_type": auth_type,
        **({"repo_id": repo_id} if repo_id is not None else {}),
        "authorization_json": json.dumps(authorization, ensure_ascii=False, sort_keys=True),
        "content_snapshot_json": json.dumps(content_snapshot, ensure_ascii=False, sort_keys=True),
        "content_version": content_version,
    }

    # Releasing an outermost SQLite savepoint commits it. Start a transaction
    # when the caller has not issued its first write yet.
    if not conn.in_transaction:
        transactions.begin_immediate(conn)
    checkpoint = "enqueue_ingress_request"
    transactions.savepoint(conn, checkpoint)
    try:
        inserted = statements.write(
            conn,
            insert(request)
            .values(**values, created_at=now, updated_at=now)
            .on_conflict_do_nothing(index_elements=[request.c.request_id]),
        )
        row = ingress_requests.find_by_id(conn, request_id)
        if inserted:
            members = statements.read(
                conn,
                select(event.c.event_id)
                .select_from(event.join(source, source.c.source_id == event.c.source_id))
                .where(
                    source.c.source_id == source_id, source.c.provider == provider, event.c.event_id.in_(identifiers)
                ),
            )
            if {member[0] for member in members} != set(identifiers):
                raise ValueError("Ingress events must belong to the requested source and provider")
            enqueue_ingress_events(conn, request_id, identifiers, now)
        elif any(row[key] != value for key, value in values.items()) or {
            member["event_id"] for member in request_events.for_ingress_request(conn, request_id)
        } != set(identifiers):
            raise ValueError("Ingress request ID already identifies a different observation")
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.release(conn, checkpoint)
    return row
