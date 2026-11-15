"""Retain canonical GitHub discussion and hold work linked to a TAPD session."""

from dataclasses import dataclass
import re

from robert_agent.adapters.github import discovery
from robert_agent.foundation import values
from robert_agent.foundation.contracts.work import SourceAuthorizationType
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import transactions
from robert_agent.persistence.queries import source_github_ingress as read, source_pr_lineage
from robert_agent.persistence.rounds.source_evidence import context_digest, json_object
from robert_agent.persistence.stores import ingress_requests, source_github_ingress as write
from robert_agent.work import source_policy
from robert_agent.work.application.tapd_intake import github_entry_grant
from robert_agent.work.tapd_authority import TicketIdentity


@dataclass(frozen=True, slots=True)
class Disposition:
    reason: str
    request_id: str | None = None


_DISCUSSION_FRAGMENTS = {"comment": "issuecomment-", "review": "pullrequestreview-", "review_comment": "discussion_r"}
_HELD = "source_github_ingress_awaiting_consumers"


def seen_event(conn, event):
    return read.event(conn, values.github_event_id(event["event_fingerprint"])) is not None


def belongs_to_native_pr(conn, repo_id, event):
    """Read-only interception for a dry-run, including an unadopted receipt."""
    repository, number = event["repo"], event["number"]
    expected = f"https://github.com/{repository}/pull/{number}"
    if event.get("url") is not None and not source_pr_lineage.same_pr_url(event["url"], expected):
        return False
    pr = source_pr_lineage.github_pr_source(conn, repository, number)
    return (
        read.association(conn, pr, repo_id) is not None
        if pr is not None
        else read.pending_creation(conn, repo_id, repository, number)
    )


def _return_target(conn, event, repo, association, event_id):
    lineage = association.lineage
    if lineage is None:
        return None
    item = event.get("github_discussion_item")
    fragment = _DISCUSSION_FRAGMENTS.get(event["event_type"])
    if not isinstance(item, dict) or fragment is None:
        return None
    identifier = item.get("id")
    if type(identifier) is int:
        identifier = str(identifier)
    if not isinstance(identifier, str) or not re.fullmatch(r"[1-9][0-9]*", identifier):
        return None
    link = item.get("url")
    if not isinstance(link, str):
        return None
    path, delimiter, anchor = link.partition("#")
    if (
        not delimiter
        or anchor != fragment + identifier
        or not source_pr_lineage.same_pr_url(path, lineage.url)
        or not source_pr_lineage.same_pr_url(event.get("url"), lineage.url)
        or event["repo"] != repo["full_name"]
        or lineage.repository != repo["full_name"]
        or event["number"] != lineage.number
        or str(event.get("id") or event.get("event_id")) != identifier
        or event["event_fingerprint"] != discovery.event_fingerprint(event["event_type"], identifier)
    ):
        return None
    tapd_source = read.source(conn, association.tapd_source_id)
    if tapd_source is None or tapd_source["provider"] != "tapd":
        return None
    try:
        ticket = TicketIdentity(
            tapd_source["host"], tapd_source["project_key"], tapd_source["source_type"], tapd_source["external_id"]
        )
    except ValueError:
        return None
    if ticket.source_id != tapd_source["source_id"]:
        return None
    return {
        "provider": "github",
        "host": "github.com",
        "repo_id": lineage.repo_id,
        "repository": lineage.repository,
        "pr_number": lineage.number,
        "pr_url": lineage.url,
        "pr_source_id": lineage.pr_source_id,
        "pr_workstream_id": lineage.workstream_id,
        "event_id": event_id,
        "event_fingerprint": event["event_fingerprint"],
        "event_type": event["event_type"],
        "item_id": identifier,
        "event_url": link,
        "tapd_source_id": ticket.source_id,
        "origin_workstream_id": lineage.origin_workstream_id,
        "creation_result_id": lineage.source_result_id,
        "creation_action_id": lineage.action_id,
        "creation_publication_id": lineage.publication_id,
        "creation_request_fingerprint": lineage.request_fingerprint,
    }


def _config_current(conn, config, repo, tapd_source_id):
    settings = (config or {}).get("tapd") or {}
    matches = [item for item in (config or {}).get("repos", ()) if item["full_name"] == repo["full_name"]]
    root = read.source(conn, tapd_source_id)
    return bool(
        settings.get("enabled") is True
        and root is not None
        and root["provider"] == "tapd"
        and settings.get("host") == root["host"]
        and len(matches) == 1
        and matches[0] == repo
        and repo.get("enabled", True) is True
        and len(
            [
                project
                for project in settings.get("projects", ())
                if project["workspace_id"] == root["project_key"] and repo["full_name"] in project["repositories"]
            ]
        )
        == 1
    )


def _observation(event, repo, *, seen):
    candidate = source_policy.reauthorize_saved_request(event, repo, known_workstreams={event["workstream_id"]})
    item = event.get("github_discussion_item")
    actor = event.get("actor_login")
    if not isinstance(item, dict):
        item = {}
    trusted = {login.casefold() for login in repo.get("trusted_actors", ())}
    body = event.get("body")
    addressed = isinstance(body, str) and bool(discovery.bot_requests(body, repo["github_account"]))
    created = utc_timestamp(item.get("created_at"))
    updated = utc_timestamp(item.get("updated_at"))
    proven_body = (
        event.get("body_observed") is True
        and isinstance(body, str)
        and created is not None
        and updated is not None
        and updated >= created
        and utc_timestamp(event.get("body_updated_at")) == updated
        and event.get("authorization_lookup_complete") is True
        and event.get("body_authorization_complete") is not False
        and (event["event_type"] != "comment" or event.get("body_authorization_complete") is True)
    )
    human = (
        isinstance(actor, str)
        and actor.casefold() != repo["github_account"].casefold()
        and not actor.casefold().endswith("[bot]")
        and item.get("author_type") in (None, "User")
        and event.get("actor_kind", "github_user") == "github_user"
    )
    authorized = (
        human
        and proven_body
        and event.get("authorization_status") in {"accepted_context", "authorized_trigger"}
        and candidate["authorization_status"] in {"accepted_context", "authorized_trigger"}
        and candidate["drives_execution"] is True
    )
    return {
        "body": body,
        "body_observed": event.get("body_observed") is True,
        "body_updated_at": event.get("body_updated_at"),
        "body_authorization_complete": event.get("body_authorization_complete"),
        "authorization_lookup_complete": event.get("authorization_lookup_complete"),
        "author_association": event.get("author_association"),
        "actor_kind": event.get("actor_kind", "github_user"),
        "body_editor_login": event.get("body_editor_login"),
        "body_edited_at": event.get("body_edited_at"),
        "event_at": event.get("event_at"),
        "actor_login": actor,
        "item": item,
        "bot_login": repo["github_account"],
        "authorization_status": event.get("authorization_status"),
        "mention_authorized": authorized,
        "addressed_to_bot": addressed,
        "actor_trusted_or_unresolved": (human and actor.casefold() in trusted)
        or (actor is None and event.get("actor_kind", "github_user") == "github_user"),
        "source_state": event.get("state"),
        "replayed": event.get("github_discussion_replayed") is True,
        "predates_bridge": seen,
        "source_context_version": event.get("source_context_version"),
    }


def _same_first_mention(first, latest):
    """Recover the original item without promoting previously observed context."""
    if (
        first.get("predates_bridge")
        or first.get("body_observed") is not True
        or first.get("addressed_to_bot") is not True
        or first.get("actor_trusted_or_unresolved") is not True
        or first.get("bot_login") != latest["bot_login"]
        or first.get("actor_login") not in (None, latest["actor_login"])
    ):
        return False
    original_item = first.get("item")
    if first.get("mention_authorized") is True and isinstance(original_item, dict) and original_item:
        # A verified first request keeps its identity while unstarted text is refreshed.
        # The caller requires current authorization and a non-regressing observation.
        return all(original_item.get(key) == latest["item"].get(key) for key in ("id", "url", "author", "created_at"))
    return (
        first.get("body") == latest["body"]
        and first.get("body_updated_at") == latest["body_updated_at"]
        and (not original_item or original_item == latest["item"])
    )


def record(conn, *, repo, repo_id, config, event, source_id, event_id, seen, now):
    """Intercept one canonical persisted event inside the intake writer."""
    existing = read.for_event(conn, event_id)
    pr_source = read.source(conn, source_id)
    association = read.association(conn, pr_source, repo_id)
    if association is None and existing is None:
        return None
    current_event = read.event(conn, event_id)
    if (
        current_event is None
        or pr_source is None
        or current_event["source_id"] != source_id
        or current_event["repo_id"] != repo_id
        or current_event["event_type"] != event["event_type"]
        or current_event["event_fingerprint"] != event["event_fingerprint"]
        or event.get("source_key") != pr_source["source_key"]
        or pr_source["number"] != event["number"]
        or pr_source["project_key"] != repo["full_name"]
        or (existing is not None and (existing["pr_source_id"] != source_id or existing["repo_id"] != repo_id))
    ):
        return Disposition("source_github_event_identity_conflict")
    target = _return_target(conn, event, repo, association, event_id) if association else None
    snapshot = _observation(event, repo, seen=seen and existing is None)
    snapshot["entry_grant"] = (
        github_entry_grant(conn, config, association.tapd_source_id, repo["full_name"])
        if association is not None and association.lineage is not None
        else {"capability": "readonly", "rule": None}
    )
    if association is None or association.lineage is None:
        reason = "source_github_lineage_unproved"
    elif not _config_current(conn, config, repo, association.tapd_source_id):
        reason = "source_github_configuration_unavailable"
    elif snapshot["replayed"]:
        reason = "source_github_observation_replayed"
    elif not target or not snapshot["mention_authorized"] or str(event.get("state", "")).casefold() != "open":
        reason = "source_github_discussion_context"
    else:
        reason = _HELD
    first = None
    if existing is not None:
        first = json_object(existing["first_observation_json"])
        prior = json_object(existing["latest_observation_json"]) or {}
        observed = utc_timestamp(now)
        previous = utc_timestamp(existing["observed_at"])
        updated = utc_timestamp(snapshot["body_updated_at"])
        prior_update = utc_timestamp(prior.get("body_updated_at"))
        if (
            snapshot["replayed"]
            or observed is None
            or previous is None
            or observed < previous
            or (first is not None and prior_update is not None and (updated is None or updated < prior_update))
        ):
            return Disposition(existing["reason"], existing["request_id"])
        # A cached body is not creator evidence. Its known source restriction
        # still caps this event when the first authenticated body arrives later.
        if first is None and prior.get("entry_grant") != snapshot["entry_grant"]:
            snapshot["entry_grant"] = {"capability": "readonly", "rule": None}

    if not conn.in_transaction:
        raise ValueError("Native GitHub ingress requires the caller-owned writer")
    existing_request = existing["request_id"] if existing is not None else None
    request_id = None
    checkpoint = "record_source_github_ingress"
    transactions.savepoint(conn, checkpoint)
    try:
        if existing_request is None and reason == _HELD:
            if read.event_already_bound(conn, event_id):
                reason = "source_github_event_already_bound"
            elif (
                (existing is None and seen)
                or (first is not None and not _same_first_mention(first, snapshot))
                or (
                    existing is not None
                    and (
                        existing["tapd_source_id"] not in (None, association.tapd_source_id)
                        or existing["pr_workstream_id"] not in (None, association.pr_workstream_id)
                        or (existing["target_json"] is not None and json_object(existing["target_json"]) != target)
                    )
                )
            ):
                reason = "source_github_event_previously_seen_or_edited"
            else:
                request_id = "github-mention:" + context_digest([source_id, event_id])
                ingress_requests.enqueue(
                    conn,
                    request_id=request_id,
                    source_id=source_id,
                    provider="github",
                    auth_type=SourceAuthorizationType.GITHUB_MENTION.value,
                    authorization={
                        "actor_login": snapshot["actor_login"],
                        "bot_login": snapshot["bot_login"],
                        "entry_grant": (first or snapshot).get("entry_grant", {"capability": "readonly", "rule": None}),
                    },
                    content_snapshot=snapshot,
                    content_version=context_digest(snapshot),
                    event_ids=(event_id,),
                    now=now,
                    repo_id=repo_id,
                )
                if not write.hold_request(
                    conn, request_id=request_id, pr_source_id=source_id, repo_id=repo_id, now=now
                ):
                    raise ValueError("Native GitHub ingress hold could not be recorded")
        if existing is None:
            written = write.insert_observation(
                conn,
                event_id=event_id,
                request_id=request_id,
                repo_id=repo_id,
                pr_source_id=source_id,
                association=association,
                target=target,
                snapshot=snapshot,
                reason=reason,
                now=now,
            )
        else:
            written = write.update_observation(
                conn,
                existing=existing,
                request_id=request_id,
                association=association,
                target=target,
                snapshot=snapshot,
                reason=reason,
                now=now,
            )
        if not written:
            raise ValueError("Native GitHub ingress bridge could not be recorded")
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.release(conn, checkpoint)
    return Disposition(reason, request_id or existing_request)
