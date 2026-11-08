"""Dispatch authority and query capabilities below the application layer."""

import json

from robert_agent.adapters.github import discovery
from robert_agent.foundation.contracts.work import DispatchRequestStatus as Status
from robert_agent.foundation import values
from robert_agent.persistence.queries import dispatch_requests as queries
from robert_agent.work.messaging_identity import dispatch_allowed


def authorized(config, row):
    return dispatch_allowed(config or {}, json.loads(row["identity_json"]), row["repo_id"].removeprefix("repo:"))


def pending(conn, config, *, remote=False, repo_ids=None):
    settings = (config or {}).get("messaging", {})
    if not settings.get("enabled") or not settings.get("dispatch_enabled"):
        return []
    allowed = {
        values.repository_id(repo["full_name"]) for repo in config["repos"] if repo["full_name"] in settings["repos"]
    }
    scoped = allowed if repo_ids is None else allowed.intersection(repo_ids)
    identities = [
        json.dumps(
            {
                **{key: settings.get(key, "") for key in ("provider", "channel", "account", "conversation", "thread")},
                "protocol": "robert.message.v1",
                "sender_id": sender,
            },
            sort_keys=True,
        )
        for sender in settings["allowed_senders"]
    ]
    statuses = [Status.ACCEPTED.value, Status.CREATING.value] if remote else [Status.ISSUE_CREATED.value]
    return [
        row
        for row in queries.list_requests(
            conn, statuses=statuses, repo_ids=scoped, identities=identities, idle_only=not remote
        )
        if authorized(config, row)
    ]


def task_allowed(conn, config, task_id):
    row = queries.for_task(conn, task_id)
    return row is None or authorized(config, row)


def reauthorize(conn, config, event):
    request_id = event.get("dispatch_request_id")
    row = queries.get(conn, request_id) if request_id else None
    if (
        not row
        or row["status"] not in {Status.ISSUE_CREATED, Status.ADMITTED, Status.DELIVERED}
        or not authorized(config, row)
    ):
        return {
            **event,
            "authorization_status": "ignored_untrusted_trigger",
            "creates_task": False,
            "drives_execution": False,
        }
    issue = json.loads(row["issue_json"])
    repo = row["repo_id"].removeprefix("repo:")
    source_key = discovery.source_key(repo, "issue", issue["number"])
    valid = (
        event.get("event_type") == "operator_dispatch"
        and event.get("actor_kind") == "operator"
        and event.get("source_key") == source_key
        and event.get("source_type") == "issue"
        and event.get("workstream_id") == source_key
        and event.get("number") == issue["number"]
        and event.get("url") == issue["html_url"]
        and event.get("event_fingerprint") == f"operator-dispatch:{request_id}"
        and event.get("body") == row["body"]
    )
    return {
        **event,
        "authorization_status": "authorized_trigger" if valid else "ignored_untrusted_trigger",
        "creates_task": valid,
        "drives_execution": valid,
    }
