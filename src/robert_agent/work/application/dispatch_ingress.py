"""Persist the original GitHub ingress associated with a confirmed operator dispatch."""

from datetime import timedelta

from robert_agent.foundation import values
from robert_agent.foundation.contracts.work import SourceAuthorizationType
from robert_agent.persistence import errors, transactions
from robert_agent.persistence.queries import dispatch_requests as queries
from robert_agent.persistence.stores import dispatch_requests as dispatch_store, ingress_requests
from robert_agent.work import dispatch_requests as facts
from robert_agent.work.application import source_events


def record_confirmed(conn, row, now):
    event = facts.confirmed_event(row)
    if event is None:
        return None
    authorization, snapshot, version = facts.ingress_snapshot(row, event)
    source_id, event_id = (
        values.github_source_id(event["source_key"]),
        values.github_event_id(event["event_fingerprint"]),
    )
    if not conn.in_transaction:
        transactions.begin_immediate(conn)
    checkpoint = "confirmed_dispatch_ingress"
    transactions.savepoint(conn, checkpoint)
    try:
        existing = queries.operator_event(conn, event_id)
        if existing is None:
            source_events.insert_source_and_event(conn, row["repo_id"], event, now, preserve_existing_source=True)
        if facts.confirmed_source(conn, row, event) is None or not facts.matches_operator_event(
            queries.operator_event(conn, event_id), row, event
        ):
            raise ValueError("Dispatch event identity conflicts with saved evidence")
        ingress_requests.enqueue(
            conn,
            request_id=row["request_id"],
            source_id=source_id,
            provider="github",
            auth_type=SourceAuthorizationType.OPERATOR_DISPATCH.value,
            authorization=authorization,
            content_snapshot=snapshot,
            content_version=version,
            event_ids=[event_id],
            now=now,
            repo_id=row["repo_id"],
        )
    except (ValueError, errors.IntegrityError):
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        return None
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.release(conn, checkpoint)
    return event


def recover_confirmed(conn, config, repo_id, now):
    """Bridge older admitted dispatch receipts before their next local audit."""
    page = facts.recovery_page(conn, config, repo_id, now)
    if not page:
        return
    for row in page[:50]:
        if record_confirmed(conn, row, now) is None:
            continue
        results = queries.accepted_final_results(conn, row["request_id"])
        if len(results) != 1:
            continue
        result = dict(results[0])
        result["consumed_event_fingerprints"] = values.json_list(result["consumed_event_fingerprints_json"])
        facts.consume_result(conn, config, row, result, now)
    more = len(page) > 50
    last = page[min(49, len(page) - 1)]
    dispatch_store.checkpoint_ingress_recovery(
        conn,
        repo_id,
        after=[last["created_at"], last["request_id"]] if more else None,
        next_at=now if more else (values.parse_time(now) + timedelta(seconds=120)).isoformat(),
    )
