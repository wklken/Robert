"""Admit saved TAPD requests under the repository lease without reading TAPD."""

import json
import secrets

from robert_agent.adapters.github import discovery as discover
from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_events as db_read_github_events
from robert_agent.persistence.queries import workstreams as db_read_workstreams
from robert_agent.persistence.stores import github_events as db_write_github_events
from robert_agent.work import source_policy
from robert_agent.work import tapd_policy

PENDING_LIMIT = 100


def _public_ref(conn, workstream_id):
    row = db_records.first(db_read_workstreams.find_public_ref_by_workstream_id(conn, workstream_id))
    return row[0] if row and row[0] else "rbt-" + secrets.token_hex(6)


def _intent(event, mention_name):
    intent = discover.infer_intent(None, event.get("body"), "comment", mention_name)
    return intent if intent in tapd_policy.DIRECT_INTENTS else None


def collect_decisions(conn, *, repo, repo_id, config_result, known_workstreams):
    """Reauthorize unprocessed TAPD requests; a request that lost authority is never retried."""
    settings = config_result.get("tapd") or {}
    if not settings.get("enabled"):
        return []
    decisions = []
    for event_id, payload_json in db_read_github_events.find_tapd_pending_triggers(conn, repo_id, PENDING_LIMIT):
        event = json.loads(payload_json)
        decision = source_policy.reauthorize_saved_request(
            event, repo, known_workstreams=known_workstreams, conn=conn, config=config_result
        )
        if not decision.get("drives_execution") or event.get("repo") != repo["full_name"]:
            status = decision["authorization_status"] if not decision.get("drives_execution") else "ignored_context"
            db_write_github_events.record_tapd_authorization(conn, event_id, status)
            continue
        decisions.append(
            {
                **decision,
                "public_ref": _public_ref(conn, decision["workstream_id"]),
                "intent": _intent(decision, settings["mention_name"]),
            }
        )
    return decisions
