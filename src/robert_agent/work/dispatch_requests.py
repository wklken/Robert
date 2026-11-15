"""Dispatch authority and query capabilities below the application layer."""

from hashlib import sha256
import json
from urllib.parse import urlsplit

from robert_agent.adapters.github import discovery
from robert_agent.foundation.contracts.work import (
    DispatchRequestStatus as Status,
    RequestEventState,
    SourceAuthorizationType,
    SourceIngressPhase,
)
from robert_agent.persistence.queries import ingress_requests, request_events, sources
from robert_agent.persistence.stores import request_events as ledger
from robert_agent.foundation import values
from robert_agent.persistence.queries import dispatch_requests as queries
from robert_agent.work.messaging_identity import dispatch_allowed, IDENTITY_FIELDS


def issue_body(row):
    return row["body"] + f"\n\n<!-- robert-dispatch request_id={row['request_id']} -->"


def confirmed_event(row):
    """Build the original operator event only from a complete confirmed Issue receipt."""
    issue = values.json_object(row["issue_json"])
    identity = values.json_object(row["identity_json"])
    repo = row["repo_id"].removeprefix("repo:")
    number = issue.get("number")
    author = issue.get("user")
    author = author.get("login") if isinstance(author, dict) else None
    url = issue.get("html_url")
    if (
        row["status"] not in {Status.ISSUE_CREATED, Status.ADMITTED, Status.DELIVERED}
        or set(identity) != {*IDENTITY_FIELDS, "protocol"}
        or identity["protocol"] != "robert.message.v1"
        or any(not isinstance(identity[key], str) for key in IDENTITY_FIELDS)
        or not identity["sender_id"]
        or type(number) is not int
        or number <= 0
        or type(issue.get("id")) is not int
        or issue["id"] <= 0
        or "pull_request" in issue
        or not isinstance(author, str)
        or not author
        or not isinstance(url, str)
        or issue.get("state") not in {"open", "closed"}
        or issue.get("body") != issue_body(row)
    ):
        return None
    try:
        location = urlsplit(url)
        valid_url = (
            location.scheme == "https"
            and location.hostname
            and location.hostname == location.netloc
            and not (location.query or location.fragment)
            and location.path.casefold() == f"/{repo}/issues/{number}".casefold()
        )
    except ValueError:
        return None
    source_key = discovery.source_key(repo, "issue", number)
    if not valid_url or row["workstream_id"] != source_key:
        return None
    return {
        "source_key": source_key,
        "workstream_id": source_key,
        "source_type": "issue",
        "number": number,
        "repo": repo,
        "repo_full_name": repo,
        "url": url,
        "event_fingerprint": f"operator-dispatch:{row['request_id']}",
        "event_type": "operator_dispatch",
        "actor_kind": "operator",
        "actor_login": None,
        "source_author_login": author,
        "dispatch_request_id": row["request_id"],
        "body": row["body"],
        "source_body": issue["body"],
        "title": row["title"],
        "state": issue["state"],
        "event_at": row["created_at"],
        "intent": "unclear",
        "body_observed": True,
        "body_authorization_complete": True,
    }


def matches_operator_event(saved, row, event):
    """Compare immutable event facts, disregarding projected authorization/lifecycle."""
    if saved is None:
        return False
    original = values.json_object(saved["payload_json"])
    return (
        saved["source_id"] == values.github_source_id(event["source_key"])
        and saved["repo_id"] == row["repo_id"]
        and saved["event_fingerprint"] == event["event_fingerprint"]
        and saved["event_type"] == event["event_type"]
        and saved["actor_kind"] == "operator"
        and saved["actor_login"] is None
        and all(original.get(key) == value for key, value in event.items() if key != "state")
    )


def ingress_snapshot(row, event):
    """Return stable identity and exact original content for this confirmed request."""
    authorization = {
        "dispatch_request_id": row["request_id"],
        "identity": values.json_object(row["identity_json"]),
        "repo_id": row["repo_id"],
        "source_key": event["source_key"],
        "issue_url": event["url"],
        "issue_id": values.json_object(row["issue_json"])["id"],
        "issue_author": event["source_author_login"],
        "event_fingerprint": event["event_fingerprint"],
    }
    content = {key: event[key] for key in ("title", "body", "source_body", "event_fingerprint")}
    version = sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return authorization, content, version


def authorized(config, row):
    return dispatch_allowed(
        config or {}, values.json_object(row["identity_json"]), row["repo_id"].removeprefix("repo:")
    )


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


def recovery_page(conn, config, repo_id, now, *, limit=51):
    """Read a due historical page, independently of Worker capacity and wakeups."""
    settings = (config or {}).get("messaging", {})
    names = {repo["full_name"] for repo in (config or {}).get("repos", []) if repo.get("enabled", True)}
    name = repo_id.removeprefix("repo:")
    if not (
        settings.get("enabled")
        and settings.get("dispatch_enabled")
        and name in settings.get("repos", [])
        and name in names
    ):
        return []
    state = values.json_object(queries.ingress_recovery_state(conn, repo_id))
    next_at = state.get("next_at")
    if isinstance(next_at, str):
        try:
            if values.parse_time(next_at) > values.parse_time(now):
                return []
        except (TypeError, ValueError):
            pass
    after = state.get("after")
    if not isinstance(after, list) or len(after) != 2 or not all(isinstance(v, str) for v in after):
        after = None
    page = queries.pending_ingress(conn, repo_id, after=after, limit=limit)
    return page or (queries.pending_ingress(conn, repo_id, limit=limit) if after else [])


def confirmed_source(conn, row, event):
    """Resolve the Issue coordinates. The confirmed receipt owns creator evidence."""
    source = sources.find_by_identity(
        conn,
        provider="github",
        host=urlsplit(event["url"]).hostname,
        project_key=event["repo"],
        source_type="issue",
        external_id=str(event["number"]),
    )
    return (
        source
        if source
        and (
            source["source_id"] == values.github_source_id(event["source_key"])
            and source["repo_id"] == row["repo_id"]
            and source["source_key"] == event["source_key"]
        )
        else None
    )


def _ingress_members(conn, row, event):
    source_id = values.github_source_id(event["source_key"])
    event_id = values.github_event_id(event["event_fingerprint"])
    authorization, snapshot, version = ingress_snapshot(row, event)
    request_id = row["request_id"]
    request = ingress_requests.find_by_id(conn, request_id)
    members = request_events.for_ingress_request(conn, request_id)
    if (
        request is None
        or request["provider"] != "github"
        or request["auth_type"] != SourceAuthorizationType.OPERATOR_DISPATCH
        or request["phase"] != SourceIngressPhase.PENDING
        or request["repo_id"] != row["repo_id"]
        or request["source_id"] != source_id
        or request["content_version"] != version
        or values.json_object(request["authorization_json"]) != authorization
        or values.json_object(request["content_snapshot_json"]) != snapshot
        or not matches_operator_event(queries.operator_event(conn, event_id), row, event)
        or len(members) != 1
        or members[0]["event_id"] != event_id
        or members[0]["source_result_id"] is not None
    ):
        return ()
    return members


def consumed_by(conn, row, result_id):
    event = confirmed_event(row)
    if event is None:
        return False
    members = _ingress_members(conn, row, event)
    return bool(
        members
        and members[0]["state"] == RequestEventState.CONSUMED
        and members[0]["result_id"] == result_id
        and members[0]["consumed_at"]
    )


def consume_result(conn, config, row, payload, now):
    """Consume the exact root request in the final result acceptance writer."""
    event = confirmed_event(row)
    if event is None or not authorized(config, row):
        return False
    source_id = values.github_source_id(event["source_key"])
    event_id = values.github_event_id(event["event_fingerprint"])
    members = _ingress_members(conn, row, event)
    source = confirmed_source(conn, row, event)
    if (
        event["state"] != "open"
        or source is None
        or source["state"] != "open"
        or not members
        or event["event_fingerprint"] not in payload["consumed_event_fingerprints"]
        or not queries.ingress_result_matches(
            conn,
            request_id=row["request_id"],
            source_id=source_id,
            event_id=event_id,
            task_id=payload["task_id"],
            attempt_id=payload["attempt_id"],
            result_id=payload["result_id"],
        )
    ):
        return False
    if members[0]["state"] == RequestEventState.CONSUMED:
        return members[0]["result_id"] == payload["result_id"] and bool(members[0]["consumed_at"])
    return ledger.consume_ingress_event_for_result(
        conn, request_id=row["request_id"], event_id=event_id, result_id=payload["result_id"], now=now
    ) == len(members)


def task_allowed(conn, config, task_id):
    row = queries.for_task(conn, task_id)
    if row is None:
        return True
    if not authorized(config, row):
        return False
    event = confirmed_event(row)
    if event is None or event["state"] != "open":
        return False
    current = confirmed_source(conn, row, event)
    members = _ingress_members(conn, row, event)
    return bool(current and current["state"] == "open" and members and members[0]["state"] != RequestEventState.REVOKED)


def reauthorize(conn, config, event):
    request_id = event.get("dispatch_request_id")
    row = queries.get(conn, request_id) if request_id else None
    canonical = confirmed_event(row) if row else None
    valid = False
    if canonical and canonical["state"] == "open" and authorized(config, row):
        current = confirmed_source(conn, row, canonical)
        fixed = (
            "event_type",
            "actor_kind",
            "actor_login",
            "source_key",
            "source_type",
            "workstream_id",
            "repo",
            "repo_full_name",
            "number",
            "url",
            "event_fingerprint",
            "body",
            "source_author_login",
        )
        members = _ingress_members(conn, row, canonical)
        valid = (
            bool(current and current["state"] == "open")
            and all(event.get(key) == canonical[key] for key in fixed)
            and bool(members and members[0]["state"] != RequestEventState.REVOKED)
        )
    return {
        **event,
        "authorization_status": "authorized_trigger" if valid else "ignored_untrusted_trigger",
        "creates_task": valid,
        "drives_execution": valid,
    }
