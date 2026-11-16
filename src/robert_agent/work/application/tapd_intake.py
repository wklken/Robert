"""Admit saved TAPD requests under the repository lease without reading TAPD."""

import json
import secrets

from robert_agent.foundation import tapd_identity, values

from robert_agent.adapters.github import discovery as discover
from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_events as db_read_github_events
from robert_agent.persistence.queries import publication_intents as db_read_publication_intents
from robert_agent.persistence.queries import workstreams as db_read_workstreams
from robert_agent.persistence.stores import github_events as db_write_github_events
from robert_agent.persistence.stores import notifications as db_write_notifications
from robert_agent.work import queries as work_queries
from robert_agent.work import source_policy
from robert_agent.work import tapd_policy

PENDING_LIMIT = 100


def _public_ref(conn, workstream_id):
    row = db_records.first(db_read_workstreams.find_public_ref_by_workstream_id(conn, workstream_id))
    return row[0] if row and row[0] else "rbt-" + secrets.token_hex(6)


def _intent(event, mention_name):
    intent = discover.infer_intent(None, event.get("body"), "comment", mention_name)
    return intent if intent in tapd_policy.DIRECT_INTENTS else None


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
    for event_id, payload_json in db_read_github_events.find_tapd_pending_triggers(conn, repo_id, PENDING_LIMIT):
        event = json.loads(payload_json)
        ticket_stream = event["source_key"]
        decision = source_policy.reauthorize_saved_request(event, repo, conn=conn, config=config_result)
        if not decision.get("drives_execution") or event.get("repo") != repo["full_name"]:
            status = decision["authorization_status"] if not decision.get("drives_execution") else "ignored_context"
            db_write_github_events.record_tapd_authorization(conn, event_id, status)
            continue
        prs = list(db_read_workstreams.find_open_derived_pull_requests(conn, repo_id, ticket_stream))
        if len(prs) > 1:
            _record_multiple_prs(conn, event, now)
            db_write_github_events.record_tapd_authorization(conn, event_id, "ignored_context")
            continue
        queue = {
            "workstream_id": ticket_stream,
            "has_open_dd_pr": False,
            "existing_pr_number": None,
            "existing_pr_head_branch": None,
            "existing_pr_url": None,
            "base_branch": None,
        }
        if prs:
            ticket_task = work_queries.active_task_id(conn, ticket_stream)
            if ticket_task and not db_records.first(
                db_read_publication_intents.find_attention_for_task(conn, ticket_task)
            ):
                # The round that opened this PR may still be confirming its ticket reply. An unknown
                # result left for the operator does not hold back the PR queue.
                continue
            stream_id, number, url, metadata = prs[0]
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
        if ticket_stream not in refs:
            refs[ticket_stream] = _public_ref(conn, ticket_stream)
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
