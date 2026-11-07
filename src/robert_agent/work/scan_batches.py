"""work scan_batches responsibilities."""

from robert_agent.persistence import database as db_database

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_scan_batches as db_read_github_scan_batches
from robert_agent.persistence.queries import github_scan_reads as db_read_github_scan_reads
from robert_agent.persistence.queries import repos as db_read_repos
from robert_agent.persistence.stores import github_scan_batches as db_write_github_scan_batches
from robert_agent.persistence.stores import github_scan_reads as db_write_github_scan_reads
from robert_agent.persistence.stores import notifications as db_write_notifications
from robert_agent.persistence.stores import wakeups as db_write_wakeups
from robert_agent.work import source_policy as source_policy
from datetime import datetime, timezone
from contextlib import closing
import json
import time
from uuid import uuid4
from robert_agent.work import wakeup
from robert_agent.adapters.notifications import wakeup as wakeup_notifier


MAX_BATCH_RETRIES = 5


def save_reads(conn, repo_id, responses, when):
    for key, payload in dict(responses).items():
        db_write_github_scan_reads.insert_for_save_reads(conn, repo_id, key, payload, when)


def save_batch(db_path, repo_id, context, raw_events, responses, *, now=None, complete_sources=()):
    when = time.time() if now is None else now
    identifier = uuid4().hex
    with closing(db_database.connect(db_path, timeout=5)) as conn, conn:
        previous = db_records.first(
            db_read_github_scan_batches.find_for_save_batch(conn, repo_id, context, json.dumps(raw_events))
        )
        if previous and previous[1] == context and previous[2] == json.dumps(raw_events):
            identifier = previous[0]
            db_write_github_scan_batches.set_complete_sources_json_by_batch_id(
                conn, json.dumps(list(complete_sources)), identifier
            )
        db_write_github_scan_batches.insert_for_save_batch(
            conn, identifier, repo_id, context, json.dumps(raw_events), when, when, json.dumps(list(complete_sources))
        )
        save_reads(conn, repo_id, responses, when)
        if not (previous and previous[3]) and db_records.first(db_read_repos.find_for_save_batch(conn, repo_id)):
            wakeup.request_wakeup(
                conn,
                repo_id=repo_id,
                reason="manual_operator_request",
                dedupe_key=identifier,
                metadata={"kind": "github_observation_ready"},
            )
    wakeup_notifier.notify(db_path)
    return identifier


def next_batch(db_path, repo_id, *, now=None, context=None):
    with closing(db_database.connect(db_path, timeout=5)) as conn:
        row = db_records.first(
            db_read_github_scan_batches.due_batch(conn, repo_id, time.time() if now is None else now, context)
        )
    if not row:
        return None
    # A newer failed read stores an explicit unknown, never an older success.
    with closing(db_database.connect(db_path, timeout=5)) as conn:
        reads = db_read_github_scan_reads.find_for_next_batch(
            conn, repo_id, (time.time() if now is None else now) - 3600
        )
    return {**dict(row), "raw_events": json.loads(row["raw_events_json"]), "responses": dict(reads)}


def has_pending(conn, repo_ids, *, now=None, contexts=None):
    when = time.time() if now is None else now
    for repo_id in repo_ids:
        if contexts is not None and repo_id not in contexts:
            continue
        if db_read_github_scan_batches.due_batch(
            conn, repo_id, when, contexts[repo_id] if contexts is not None else None
        ):
            return True
    return False


def fail_batch(conn, batch_id, reason, when):
    row = db_records.first(db_read_github_scan_batches.find_for_fail_batch(conn, batch_id))
    if not row or row[2]:
        return
    count = row[1] + 1
    attention = reason if count >= MAX_BATCH_RETRIES else None
    retry_at = when + min(300, 30 * (2 ** (count - 1)))
    db_write_github_scan_batches.update_for_fail_batch(conn, count, attention, retry_at, batch_id)
    db_write_wakeups.update_for_fail_batch(
        conn,
        "canceled" if attention else "pending",
        datetime.fromtimestamp(retry_at, timezone.utc).isoformat(),
        datetime.fromtimestamp(when, timezone.utc).isoformat(),
        batch_id,
    )
    if attention:
        db_write_notifications.insert_for_fail_batch(
            conn,
            "github-scan:" + batch_id,
            datetime.fromtimestamp(when, timezone.utc).isoformat(),
            json.dumps(
                {
                    "repo_id": row[0],
                    "batch_id": batch_id,
                    "reason_code": reason,
                    "retry_count": count,
                    "required_action": f"Inspect GitHub batch {batch_id} (attention at {when}); restore access and explicitly retry.",
                }
            ),
        )


def retry_batch(db_path, batch_id, repo_ids, *, now=None):
    when = time.time() if now is None else now
    with closing(db_database.connect(db_path, timeout=5)) as conn, conn:
        row = db_records.first(db_read_github_scan_batches.find_for_retry_batch(conn, batch_id))
        if not row or row[0] not in repo_ids or not row[1]:
            return {"ok": False, "status": "not_in_scope_or_not_attention"}
        db_write_github_scan_batches.update_for_retry_batch(conn, when, batch_id)
        db_write_wakeups.update_for_retry_batch(conn, datetime.fromtimestamp(when, timezone.utc).isoformat(), batch_id)
        db_write_notifications.update_for_persist_control_failure_alerts(conn, "github-scan:" + batch_id)
    wakeup_notifier.notify(db_path)
    return {"ok": True, "status": "retry_requested", "batch_id": batch_id}


def finish_batch(db_path, batch_id, *, success, now=None, failure_reason="local_processing_failed"):
    when = time.time() if now is None else now
    with closing(db_database.connect(db_path, timeout=5)) as conn, conn:
        if success:
            current = db_records.first(db_read_github_scan_batches.find_for_finish_batch(conn, batch_id))
            if current:
                covered = {tuple(source) for source in json.loads(current[3])}
                replayed = {source_policy.event_identity(raw) for raw in json.loads(current[4])}
                older = db_read_github_scan_batches.find_for_finish_batch_2(conn, current[0], current[1], current[2])
                for old_id, encoded in older:
                    sources = {(event.get("source_type", "issue"), event["number"]) for event in json.loads(encoded)}
                    if (
                        sources <= covered
                        and {source_policy.event_identity(raw) for raw in json.loads(encoded)} <= replayed
                    ):
                        db_write_github_scan_batches.set_completed_at_by_batch_id(conn, when, old_id)
                        db_write_wakeups.update_for_finish_batch(conn, old_id)
            db_write_github_scan_batches.set_completed_at_by_batch_id(conn, when, batch_id)
            db_write_wakeups.update_for_finish_batch(conn, batch_id)
        else:
            fail_batch(conn, batch_id, failure_reason, when)
        db_write_wakeups.delete_for_finish_batch(conn)
        db_write_github_scan_batches.delete_for_finish_batch(conn)
        db_write_github_scan_reads.delete_for_finish_batch(conn, when - 3600)
