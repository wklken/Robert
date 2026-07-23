#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

from robert_agent.common import emit


ACCEPTED_CONTEXT_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
ACCEPTED_CONTEXT_PERMISSIONS = {"admin", "maintain", "write", "triage"}


def _decision(event, status, creates_task=False, drives_execution=False):
    return {
        **event,
        "authorization_status": status,
        "creates_task": bool(creates_task),
        "drives_execution": bool(drives_execution),
    }


def authorize_event(event, repo_config, existing_workstreams=None, known_workstreams=None):
    if known_workstreams is None:
        known_workstreams = existing_workstreams
    known_workstreams = set(known_workstreams or [])
    trusted_actors = set(repo_config.get("trusted_actors", []))
    github_account = repo_config["github_account"]
    actor = event.get("actor_login")
    event_type = event.get("event_type")

    if event_type == "assigned" and event.get("assigned_to") == github_account:
        assignment_actor = event.get("assignment_actor_login")
        if not assignment_actor:
            return _decision(event, "pending_authorization")
        if assignment_actor in trusted_actors:
            return _decision(event, "authorized_trigger", creates_task=True, drives_execution=True)
        return _decision(event, "ignored_untrusted_trigger")

    if actor == github_account:
        return _decision(event, "context_only")

    if event.get("workstream_id") in known_workstreams:
        if not event.get("mentions_dd"):
            return _decision(event, "ignored_context")
        if actor in trusted_actors:
            return _decision(event, "accepted_context", drives_execution=True)
        if event.get("source_type") == "pull_request" and actor in set(event.get("review_participants") or []):
            return _decision(event, "accepted_review_participant", drives_execution=True)
        association = event.get("author_association")
        if association in ACCEPTED_CONTEXT_ASSOCIATIONS:
            return _decision(event, "accepted_context", drives_execution=True)
        permission = (event.get("actor_permission") or "").lower()
        if permission in ACCEPTED_CONTEXT_PERMISSIONS:
            return _decision(event, "accepted_context", drives_execution=True)
        if association in {None, "", "UNKNOWN"}:
            return _decision(event, "pending_actor_permission")
        return _decision(event, "ignored_context")

    if event_type == "notification":
        if event.get("trusted_trigger_found"):
            return _decision(event, "authorized_trigger", creates_task=True, drives_execution=True)
        if not event.get("authorization_lookup_complete"):
            return _decision(event, "pending_authorization")
        return _decision(event, "ignored_untrusted_trigger")

    requested_review = (
        event.get("requested_review_dd")
        or event.get("requested_reviewer") == github_account
        or event.get("requested_team") == github_account
    )
    if event_type == "review_request" and requested_review:
        requester = event.get("requester_login") or actor
        if requester in trusted_actors:
            return _decision(event, "authorized_trigger", creates_task=True, drives_execution=True)
        return _decision(event, "ignored_untrusted_trigger")

    if event.get("mentions_dd"):
        if event.get("authorization_lookup_complete") is False:
            return _decision(event, "pending_authorization")
        if actor in trusted_actors:
            return _decision(event, "authorized_trigger", creates_task=True, drives_execution=True)
        return _decision(event, "ignored_untrusted_trigger")

    return _decision(event, "ignored_context")


def authorize_events(events, repo_config, existing_workstreams=None, known_workstreams=None):
    return [
        authorize_event(
            event,
            repo_config,
            existing_workstreams=existing_workstreams,
            known_workstreams=known_workstreams,
        )
        for event in events
    ]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-config", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--existing-workstream", action="append", default=[])
    args = parser.parse_args(argv)

    repo_config = json.loads(Path(args.repo_config).read_text(encoding="utf-8"))
    events = json.loads(Path(args.events).read_text(encoding="utf-8"))
    decisions = authorize_events(
        events,
        repo_config,
        existing_workstreams=set(args.existing_workstream),
    )
    return emit({"ok": True, "status": "authorized", "decisions": decisions})


if __name__ == "__main__":
    raise SystemExit(main())
