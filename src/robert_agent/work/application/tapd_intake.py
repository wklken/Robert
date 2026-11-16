"""Admit saved TAPD requests under the repository lease without reading TAPD."""

import json
import secrets

from robert_agent.foundation import tapd_identity, values

from robert_agent.adapters.github import discovery as discover
from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_events as db_read_github_events
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import workstreams as db_read_workstreams
from robert_agent.persistence.stores import github_events as db_write_github_events
from robert_agent.persistence.stores import notifications as db_write_notifications
from robert_agent.persistence.stores import workstreams as db_write_workstreams
from robert_agent.work import backports
from robert_agent.work import queries as work_queries
from robert_agent.work import source_policy
from robert_agent.work import tapd_policy

PENDING_LIMIT = 100


def _public_ref(conn, workstream_id):
    row = db_records.first(db_read_workstreams.find_public_ref_by_workstream_id(conn, workstream_id))
    return row[0] if row and row[0] else "rbt-" + secrets.token_hex(6)


def _intent(event, mention_name):
    if event.get("backport"):
        return "bug_fix"
    intent = discover.infer_intent(None, event.get("body"), "comment", mention_name)
    return intent if intent in tapd_policy.DIRECT_INTENTS else None


def expand_backport(conn, *, event, request, repo_id, public_ref, now):
    """Save every target before disposing the command; pending targets survive partial dispatch."""
    source_id = values.github_source_id(event["source_key"])
    ticket_stream = event["source_key"]
    db_write_workstreams.ensure_tapd_delivery_stream(
        conn,
        workstream_id=ticket_stream,
        repo_id=repo_id,
        source_id=source_id,
        origin_id=None,
        public_ref=public_ref,
        now=now,
    )
    fingerprints = []
    for base in request.targets:
        fingerprint = event["event_fingerprint"] + ":backport:" + backports.target_key(base)
        child = {
            **event,
            "event_fingerprint": fingerprint,
            "backport": {"source_pr": request.source_pr, "target_base_branch": base},
            "public_ref": public_ref,
        }
        db_write_github_events.insert_tapd_event_if_absent(
            conn,
            values.github_event_id(fingerprint),
            repo_id,
            source_id,
            fingerprint,
            event["event_type"],
            event["actor_login"],
            "authorized_trigger",
            event.get("event_at", now),
            json.dumps(child, sort_keys=True),
        )
        fingerprints.append(fingerprint)
    return fingerprints


def _record_multiple_prs(conn, event, now):
    fingerprint = event["event_fingerprint"]
    kind = tapd_policy.MULTIPLE_PRS_NOTICE
    metadata = {
        "dedupe_key": fingerprint,
        "kind": tapd_identity.KINDS[event["source_type"]],
        **{field: event[field] for field in ("project_key", "external_id", "url")},
    }
    db_write_notifications.insert_tapd_notification(
        conn, tapd_policy.notice_id(kind, fingerprint), kind, now, json.dumps(metadata, sort_keys=True)
    )


def collect_decisions(conn, *, repo, repo_id, config_result, known_workstreams, now):
    """Reauthorize unprocessed TAPD requests; a request that lost authority is never retried."""
    settings = config_result.get("tapd") or {}
    if not settings.get("enabled"):
        return []
    decisions = []
    refs = {}
    pending = list(db_read_github_events.find_tapd_pending_triggers(conn, repo_id, PENDING_LIMIT))
    for event_id, payload_json in pending:
        event = json.loads(payload_json)
        ticket_stream = event["source_key"]
        decision = source_policy.reauthorize_saved_request(event, repo, conn=conn, config=config_result)
        if not decision.get("drives_execution") or event.get("repo") != repo["full_name"]:
            status = decision["authorization_status"] if not decision.get("drives_execution") else "ignored_context"
            db_write_github_events.record_tapd_authorization(conn, event_id, status)
            continue
        if ticket_stream not in refs:
            refs[ticket_stream] = _public_ref(conn, ticket_stream)
        # The display body has lost quote/code boundaries and cannot authorize target branches.
        saved_request = event.get("backport_request")
        request = (
            backports.BackportRequest(saved_request["source_pr"], tuple(saved_request["targets"]))
            if saved_request
            else None
        )
        if request and not event.get("backport"):
            fingerprints = expand_backport(
                conn,
                event=event,
                request=request,
                repo_id=repo_id,
                public_ref=refs[ticket_stream],
                now=now,
            )
            db_write_github_events.record_tapd_authorization(conn, event_id, "context_only")
            for fingerprint in fingerprints:
                child_id = values.github_event_id(fingerprint)
                row = db_records.first(db_read_github_events.find_payload_json_by_event_fingerprint(conn, fingerprint))
                pending.append((child_id, row[0]))
            continue
        backport = event.get("backport") or {}
        target = backport.get("target_base_branch")
        prs = list(
            db_read_workstreams.find_open_derived_pull_requests(
                conn,
                repo_id,
                ticket_stream,
                base_branch=target,
            )
        )
        if len(prs) > 1:
            _record_multiple_prs(conn, event, now)
            db_write_github_events.record_tapd_authorization(conn, event_id, "ignored_context")
            continue
        queue = {
            "workstream_id": backports.target_stream(ticket_stream, target) if target else ticket_stream,
            "has_open_dd_pr": False,
            "existing_pr_number": None,
            "existing_pr_head_branch": None,
            "existing_pr_url": None,
            "base_branch": target,
        }
        if target and not prs:
            db_write_workstreams.ensure_tapd_delivery_stream(
                conn,
                workstream_id=queue["workstream_id"],
                repo_id=repo_id,
                source_id=values.github_source_id(event["source_key"]),
                origin_id=ticket_stream,
                public_ref=None,
                now=now,
            )
        if prs:
            stream_id, number, url, metadata = prs[0]
            opening = (
                db_records.first(db_read_tasks.find_active_tapd_opening_task(conn, ticket_stream, url))
                if target
                else None
            )
            opening_stream = backports.target_stream(ticket_stream, target) if target else ticket_stream
            opening_stream = opening[0] if opening else opening_stream
            ticket_task = opening[1] if opening else work_queries.active_task_id(conn, opening_stream)
            if ticket_task and not work_queries.tapd_reply_in_attention(
                conn, opening_stream, ticket_task, event["url"]
            ):
                # The round that opened this PR may still be confirming its ticket reply. An unknown
                # result left for the operator does not hold back the PR queue.
                continue
            receipt = values.json_object(metadata)
            if not receipt.get("head") or not receipt.get("base"):
                continue
            queue.update(
                workstream_id=stream_id,
                has_open_dd_pr=True,
                existing_pr_number=number,
                existing_pr_head_branch=receipt["head"].split(":", 1)[-1],
                existing_pr_url=url,
                base_branch=receipt["base"],
            )
        decisions.append(
            {
                **decision,
                **queue,
                "authorization_status": "accepted_context"
                if queue["workstream_id"] in known_workstreams or queue["has_open_dd_pr"]
                else "authorized_trigger",
                "creates_task": not (queue["workstream_id"] in known_workstreams or queue["has_open_dd_pr"]),
                "public_ref": refs[ticket_stream],
                "intent": _intent(decision, settings["mention_name"]),
            }
        )
    return decisions
