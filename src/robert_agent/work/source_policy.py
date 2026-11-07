"""Stable identity and current-authority policy for preserved source events."""

from robert_agent.security import authorization as authorize
from robert_agent.adapters.github import discovery as discover


def event_identity(raw):
    return (
        raw.get("source_type", "issue"),
        raw.get("number"),
        raw.get("event_fingerprint") or f"{raw.get('event_type')}:{raw.get('id') or raw.get('event_id')}",
    )


def refresh_pending_events(events, pending, covered, repo):
    """Carry every unconsumed identity across context changes, after a fresh read.

    Authorization still runs locally. Never carry a derived trust boolean from
    the old configuration into the new context.
    """
    merged = {event_identity(raw): raw for raw in events}
    trusted = set(repo.get("trusted_actors", []))
    account = repo["github_account"]
    for raw in pending:
        if (raw.get("source_type", "issue"), raw["number"]) not in covered:
            continue
        refreshed = dict(raw)
        refreshed["mentions_dd"] = f"@{account}" in (raw.get("body") or "")
        if raw.get("event_type") == "notification":
            refreshed["trusted_trigger_found"] = bool(
                raw.get("authorization_lookup_complete") is not False
                and (
                    (raw.get("assigned_to") == account and raw.get("assignment_actor_login") in trusted)
                    or (raw.get("requested_reviewer") == account and raw.get("requester_login") in trusted)
                    or (refreshed["mentions_dd"] and raw.get("actor_login") in trusted)
                )
            )
        merged.setdefault(event_identity(raw), refreshed)
    return list(merged.values())


def reauthorize_saved_request(event, repo, *, known_workstreams=()):
    candidate = {
        **event,
        "mentions_dd": bool(discover.bot_requests(event.get("body"), repo["github_account"])),
    }
    if candidate.get("event_type") == "notification":
        trusted = set(repo.get("trusted_actors", []))
        candidate["trusted_trigger_found"] = bool(
            candidate.get("authorization_lookup_complete") is not False
            and (
                (
                    candidate.get("assigned_to") == repo["github_account"]
                    and candidate.get("assignment_actor_login") in trusted
                )
                or (
                    candidate.get("requested_reviewer") == repo["github_account"]
                    and candidate.get("requester_login") in trusted
                )
                or (candidate["mentions_dd"] and candidate.get("actor_login") in trusted)
            )
        )
    return authorize.authorize_event(candidate, repo, known_workstreams=set(known_workstreams))
