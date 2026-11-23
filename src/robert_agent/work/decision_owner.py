"""The original accepted request owns human decisions throughout its PR lineage."""

import json
import re

from robert_agent.foundation import values
from robert_agent.persistence import records
from robert_agent.persistence.queries import workstreams as queries
from robert_agent.persistence.stores import workstreams as stores


def _login(value, github_account):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", value):
        return None
    if value.casefold() in {github_account.casefold(), "github"}:
        return None
    return value


def from_event(event, github_account):
    """Consume authorization facts, never usernames embedded in request prose."""
    if (
        event.get("provider", "github") != "github"
        or event.get("authorization_status") not in {"authorized_trigger", "accepted_context"}
        or not event.get("drives_execution")
        or event.get("actor_kind") == "github_system"
        or event.get("event_type") == "proposal_approved"
        or event.get("authorization_lookup_complete") is False
        or event.get("body_authorization_complete") is False
    ):
        return None
    kind = event.get("event_type")
    if kind == "assigned" or (kind == "notification" and event.get("assigned_to")):
        actor = event.get("assignment_actor_login")
    elif kind == "review_request" or (kind == "notification" and event.get("requester_login")):
        actor = event.get("requester_login") or event.get("actor_login")
    else:
        actor = event.get("actor_login")
    login = _login(actor, github_account)
    if not login or not event.get("source_key") or not event.get("event_fingerprint"):
        return None
    return {"login": login, "source_key": event["source_key"], "event_fingerprint": event["event_fingerprint"]}


def _stored_owner(metadata, github_account):
    owner = metadata.get("decision_owner")
    if isinstance(owner, dict) and _login(owner.get("login"), github_account):
        if owner.get("source_key") and owner.get("event_fingerprint"):
            return dict(owner)
    return None


def _root(conn, workstream_id):
    seen, repo_id = set(), None
    while workstream_id and workstream_id not in seen:
        seen.add(workstream_id)
        row = records.first(queries.decision_owner_stream(conn, workstream_id))
        if not row or (repo_id is not None and row[0] != repo_id):
            return None
        repo_id = row[0]
        if not row[1]:
            return workstream_id, row
        workstream_id = row[1]
    return None


def for_workstream(conn, workstream_id, github_account):
    """Read the fixed owner, recovering legacy provenance without changing history."""
    root = _root(conn, workstream_id)
    if not root:
        return None
    root_id, row = root
    metadata = values.json_object(row[2])
    if "decision_owner" in metadata:
        return _stored_owner(metadata, github_account)
    request = records.first(queries.decision_owner_requests(conn, root_id, row[0]))
    return from_event(values.json_object(request[0]), github_account) if request else None


def record(conn, workstream_id, event, github_account, now):
    """Fix ownership under the caller's transaction; later participants cannot replace it."""
    row = records.first(queries.decision_owner_stream(conn, workstream_id))
    if not row:
        return None
    metadata = values.json_object(row[2])
    origin = event.get("origin_workstream_id")
    if origin and not row[1] and "decision_owner" not in metadata:
        # Discovery accepts lineage markers only from the configured publisher.
        if (event.get("pr_author_login") or "").casefold() == github_account.casefold():
            parent = records.first(queries.decision_owner_stream(conn, origin))
            if parent and parent[0] == row[0] and origin != workstream_id:
                stores.set_decision_owner_origin(conn, workstream_id, origin)
                row = records.first(queries.decision_owner_stream(conn, workstream_id))
            else:
                metadata["decision_owner"] = None
    if row[1]:
        return for_workstream(conn, workstream_id, github_account)
    if "decision_owner" in metadata:
        owner = _stored_owner(metadata, github_account)
    else:
        first = records.first(queries.decision_owner_requests(conn, workstream_id, row[0]))
        owner = from_event(values.json_object(first[0]) if first else event, github_account)
        metadata["decision_owner"] = owner
    stores.set_metadata_json_and_updated_at_by_workstream_id(
        conn, json.dumps(metadata, ensure_ascii=False), now, workstream_id
    )
    return owner
