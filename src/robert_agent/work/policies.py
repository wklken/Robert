"""Pure event authority predicates; no discovery or scheduling effects."""


def is_driving_context_event(event):
    return event.get("authorization_status") in {
        "accepted_context",
        "accepted_review_participant",
    } and event.get("drives_execution")


def is_trusted_actor(event, repo):
    return event.get("actor_login") in set(repo.get("trusted_actors") or [])
