"""Pure event authority predicates; no discovery or scheduling effects."""

from robert_agent.foundation.contracts.work import SourceProvider


def is_driving_context_event(event):
    return event.get("authorization_status") in {
        "accepted_context",
        "accepted_review_participant",
    } and event.get("drives_execution")


def is_trusted_actor(event, repo):
    if event.get("provider") == SourceProvider.TAPD:
        # Set by TAPD reauthorization from current configuration; GitHub trusted actors never apply.
        trusted = event.get("tapd_trusted_user")
        return bool(trusted) and event.get("actor_login") == trusted
    return (event.get("actor_login") or "").casefold() in {
        login.casefold() for login in repo.get("trusted_actors") or []
    }
