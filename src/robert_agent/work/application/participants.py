"""Track trusted reviewers by workstream."""

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import workstreams as db_read_workstreams
from robert_agent.persistence.stores import workstreams as db_write_workstreams
from robert_agent.foundation import values
import json


def load_review_participants(conn, repo_id):
    participants = {}
    rows = db_read_workstreams.load_review_participants(conn, repo_id)
    for workstream_id, metadata_json in rows:
        metadata = values.json_object(metadata_json)
        review_participants = metadata.get("review_participants")
        if not isinstance(review_participants, list):
            continue
        scoped = [login for login in review_participants if isinstance(login, str) and login]
        if scoped:
            participants[workstream_id] = scoped
    return participants


def attach_review_participants(events, review_participants_by_workstream):
    enriched = []
    for event in events:
        participants = review_participants_by_workstream.get(event.get("workstream_id"))
        if participants:
            enriched.append({**event, "review_participants": participants})
        else:
            enriched.append(event)
    return enriched


def record_review_participant(conn, workstream_id, event, route_result, run_now):
    if route_result.get("route_id") != "review-pr":
        return
    pr_author = event.get("pr_author_login")
    if not pr_author:
        return
    row = db_records.first(db_read_workstreams.find_metadata_json_by_workstream_id(conn, workstream_id))
    metadata = values.json_object(row[0] if row else None)
    participants = metadata.get("review_participants")
    if not isinstance(participants, list):
        participants = []
    if pr_author not in participants:
        participants.append(pr_author)
    metadata["review_participants"] = participants
    metadata["review_authorized_by"] = event.get("requester_login") or event.get("actor_login") or ""
    metadata["review_authorized_event"] = event.get("event_fingerprint") or ""
    metadata["review_authorized_at"] = event.get("event_at") or run_now
    db_write_workstreams.set_metadata_json_and_updated_at_by_workstream_id(
        conn, json.dumps(metadata, ensure_ascii=False, sort_keys=True), run_now, workstream_id
    )
