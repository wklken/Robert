"""Handle GitHub request priorities and native-round admission."""

from robert_agent.adapters.github import discovery as discover
from robert_agent.work.application import source_events
from robert_agent.execution import ownership as execution_ownership
from robert_agent.foundation import values
from robert_agent.supervision import lineage as supervision_lineage
from robert_agent.work import policies as work_policies
from robert_agent.adapters.sqlite import vote_rounds as vote_store
import json


def event_priority(event):
    event_at = event.get("event_at") or ""
    fingerprint = event.get("event_fingerprint") or ""
    if event.get("authorization_status") == "authorized_system_trigger":
        episode_kind = ((event.get("metadata") or {}).get("remediation") or {}).get("episode_kind")
        return (
            -1,
            0 if episode_kind == "merge_conflict" else 1,
            event_at,
            fingerprint,
        )
    if event.get("authorization_status") == "authorized_trigger":
        if event.get("source_type") == "pull_request" and event.get("has_open_dd_pr"):
            return (0, event_at, fingerprint)
        return (1, event_at, fingerprint)
    if work_policies.is_driving_context_event(event):
        return (2, event_at, fingerprint)
    return (3, event_at, fingerprint)


def is_dd_pr_followup_event(event):
    metadata = event.get("metadata") or {}
    return (
        event.get("source_type") == "pull_request"
        and event.get("has_open_dd_pr")
        and bool(event.get("origin_workstream_id") or metadata.get("dd_workstream"))
    )


def dedupe_events_by_fingerprint(events):
    deduped = []
    seen = set()
    for event in events:
        fingerprint = event.get("event_fingerprint")
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(event)
    return deduped


def enqueue_native_request(conn, store, repo, event, route, workers, admission, now):
    lineage = supervision_lineage.github_key(event)
    round_entry = store.enqueue(
        conn,
        repo,
        {**event, supervision_lineage.LINEAGE_FIELD: lineage},
        route,
        workers,
    )
    supervision_lineage.record_disposition(
        conn,
        admission,
        disposition="queued_native_round",
        now=now,
        target_type="vote_round" if store is vote_store else "review_round",
        target_id=round_entry["round_id"],
        lineage_key=lineage,
    )


def resume_waiting_vote(conn, repo, event, run_now, admission=None):
    if (
        event.get("actor_login") not in repo.get("trusted_actors", [])
        or not event.get("mentions_dd")
        or not discover.bot_requests(event.get("body"), repo["github_account"])
        or event.get("authorization_status") not in {"accepted_context", "authorized_trigger"}
        or event.get("intent") not in {None, "unclear", "analysis", "discussion", "requirement_analysis"}
    ):
        return False
    row = conn.execute(
        """SELECT vs.round_id,vs.task_id FROM vote_sessions vs JOIN tasks t ON t.task_id=vs.task_id
        WHERE vs.workstream_id=? AND vs.status='waiting_for_user' AND t.lifecycle='waiting_for_user'
        AND vs.source_type=? AND vs.number=?""",
        (event["workstream_id"], event["source_type"], event["number"]),
    ).fetchone()
    if not row:
        return False
    if source_events.event_has_terminal_trigger(conn, event["event_fingerprint"]):
        supervision_lineage.record_disposition(
            conn,
            admission,
            disposition="explicitly_ignored",
            now=run_now,
            reason_code="trigger_already_processed",
        )
        return True  # Already-used replies cannot answer another question on polling.
    event_time = event.get("event_at")
    if not event_time:
        observed = conn.execute(
            "SELECT event_at FROM github_events WHERE event_id=?",
            (values.github_event_id(event["event_fingerprint"]),),
        ).fetchone()
        event_time = observed[0] if observed else None
    question = conn.execute(
        "SELECT created_at FROM worker_results WHERE task_id=? AND output_type='waiting_for_user' ORDER BY created_at DESC LIMIT 1",
        (row[1],),
    ).fetchone()
    try:
        # GitHub comment times have second precision; avoid a false rejection due to local microseconds.
        if (
            not event_time
            or not question
            or values.parse_time(event_time) < values.parse_time(question[0]).replace(microsecond=0)
        ):
            supervision_lineage.record_disposition(
                conn,
                admission,
                disposition="explicitly_ignored",
                now=run_now,
                reason_code="reply_does_not_follow_question",
            )
            return True
    except (ValueError, TypeError):
        return True
    entry = vote_store.session(conn, round_id=row[0])
    if entry["manifest"].get("frozen"):
        return False
    request = entry["request"]
    if any(
        item.get("event_fingerprint") == event["event_fingerprint"]
        for item in request["event"].get("clarifications", [])
    ):
        supervision_lineage.record_disposition(
            conn,
            admission,
            disposition="queued_native_round",
            now=run_now,
            target_type="vote_round",
            target_id=row[0],
            lineage_key=request["event"].get(supervision_lineage.LINEAGE_FIELD),
        )
        return True
    request["event"].setdefault("clarifications", []).append(event)
    conn.execute(
        "UPDATE tasks SET lifecycle='completed',updated_at=? WHERE task_id=?",
        (run_now, row[1]),
    )
    execution_ownership.close_terminal_task(conn, row[1], run_now)
    conn.execute(
        "UPDATE workstreams SET lifecycle='completed',active_task_id=NULL,updated_at=? WHERE workstream_id=?",
        (run_now, event["workstream_id"]),
    )
    vote_store.update(
        conn,
        row[0],
        task_id=None,
        status="queued",
        manifest_json=None,
        request_json=json.dumps(request, ensure_ascii=False),
    )
    conn.execute(
        "INSERT OR IGNORE INTO task_events(task_id,event_id,relationship,created_at) VALUES (?,?,'consumed',?)",
        (row[1], values.github_event_id(event["event_fingerprint"]), run_now),
    )
    supervision_lineage.record_disposition(
        conn,
        admission,
        disposition="queued_native_round",
        now=run_now,
        target_type="vote_round",
        target_id=row[0],
        lineage_key=request["event"].get(supervision_lineage.LINEAGE_FIELD),
    )
    return True
