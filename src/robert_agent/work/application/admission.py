"""Reauthorize preserved requests and append input under execution ownership."""

from dataclasses import dataclass

from robert_agent.delivery.application import validation as result_validation
from robert_agent.foundation.contracts.delivery import NATIVE_RESULT_CONTRACT
from robert_agent.foundation.contracts.work import TaskKind

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import result_contract as result_queries
from robert_agent.persistence.queries import (
    source_members,
    source_pr_selection,
    source_sessions as round_queries,
    source_tasks,
)
from robert_agent.persistence.stores import source_sessions
from robert_agent.persistence.stores.source_sessions import SourceClaim
from robert_agent.persistence.rounds.source_evidence import context_digest
from robert_agent.work.application import source_github_observation, tapd_intake
from robert_agent.work.tapd_authority import PublicationFacts, TicketIdentity, TicketObservation
from robert_agent.persistence.queries import task_events as db_read_task_events
from robert_agent.persistence.queries import work_items as db_read_work_items
from robert_agent.persistence.stores import task_events as db_write_task_events
from robert_agent.persistence.stores import workstreams as db_write_workstreams
import json
from robert_agent.work import source_policy, dispatch_requests
from robert_agent.execution import ownership as execution_ownership
from robert_agent.work import queries as work_queries


def append_event_to_active_task(conn, event, relationship, run_now):
    task_id = work_queries.active_task_id(conn, event["workstream_id"])
    if not task_id:
        return False
    if not execution_ownership.claim_task(conn, task_id, run_now):
        return False
    db_write_task_events.append_task_event(conn, task_id, f"event:{event['event_fingerprint']}", relationship, run_now)
    db_write_workstreams.set_updated_at_by_workstream_id(conn, run_now, event["workstream_id"])
    return True


def _json_object(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def ordinary_dispatch_allowed(conn, task_id, repo, config=None):
    if source_tasks.has_native_origin(conn, task_id):
        return False
    row = db_records.first(db_read_tasks.find_task_kind_and_metadata_json_by_task_id(conn, task_id))
    if not row or row[0] != TaskKind.HUMAN_REQUEST.value:
        return True  # System remediation is governed by its Episode policy.
    if not dispatch_requests.task_allowed(conn, config, task_id):
        return False
    metadata = _json_object(row[1])
    item = db_records.first(db_read_work_items.find_for_ordinary_dispatch_allowed(conn, task_id))
    if (item and item[0] == "web") or metadata.get("trigger_work_item_event_id"):
        return True
    events = work_queries.task_related_events(conn, task_id, include_consumed=True)
    fingerprint = metadata.get("request_event_fingerprint")
    if fingerprint:
        events = [event for event in events if event.get("event_fingerprint") == fingerprint]
    else:
        # Older tasks lack the explicit binding. Only a surviving trigger
        # relationship can identify their authority; arbitrary context cannot.
        triggers = {row[1] for row in db_read_task_events.task_related_events_filtered(conn, ["trigger"], task_id)}
        if len(triggers) != 1:
            return False
        events = [event for event in events if event.get("event_fingerprint") in triggers]
    if not events:
        return False
    return any(
        source_policy.reauthorize_saved_request(
            event,
            repo,
            conn=conn,
            config=config,
            known_workstreams=[]
            if event.get("event_type") in {"assigned", "review_request", "notification"}
            else [event["workstream_id"]],
        ).get("drives_execution")
        for event in events
    )


def native_round_authority(
    conn,
    config,
    claim,
    *,
    round_id,
    revision,
    repo_id,
    capability,
    now,
    task_id=None,
    mixed_proof=None,
    target=None,
):
    """Check the persisted current batch and source fence under the caller's writer."""
    tapd = (config or {}).get("tapd") or {}
    if tapd.get("enabled") is not True or not revision or capability not in {"readonly", "development"}:
        return None
    active = source_sessions.lock_round(conn, claim, round_id, now=now)
    if (
        active is None
        or repo_id not in json.loads(active["repo_ids_json"])
        or (capability == "development" and active["capability"] != "development")
        or round_queries.blockers_for_source(conn, claim.source_id, limit=1, queued_task_id=task_id)
    ):
        return None
    scope = _json_object(active["publication_scope_json"])
    try:
        ticket = TicketIdentity(scope["host"], scope["workspace_id"], scope["kind"], scope["external_id"])
    except (ValueError, KeyError, TypeError):
        return None
    repo = source_tasks.repository(conn, repo_id)
    if ticket.source_id != claim.source_id or repo is None:
        return None
    matches = [item for item in config["repos"] if item["full_name"] == repo["full_name"]]
    if len(matches) != 1 or matches[0].get("enabled", True) is not True:
        return None
    configured_repo = matches[0]
    if any(
        configured_repo.get(field) != repo[field]
        for field in ("repo_root", "worktree_root", "default_base_branch", "github_account")
    ):
        return None
    projects = [project for project in tapd["projects"] if project["workspace_id"] == ticket.workspace_id]
    if tapd.get("host") != ticket.host or len(projects) != 1 or repo["full_name"] not in projects[0]["repositories"]:
        return None
    members = source_sessions.pending_round_members(conn, round_id)
    if not members:
        return None
    github_ids = source_members.round_github_request_ids(conn, round_id)
    github = None
    if github_ids:
        github = tapd_intake.current_mixed_worker_grants(
            conn,
            config,
            ticket,
            claim,
            active,
            revision=revision,
            proof=mixed_proof,
            now=now,
            target=target,
        )
        if github is None:
            return None
    elif mixed_proof is not None:
        return None
    for member in members:
        current = (
            github.get(member["request_id"])
            if member["request_id"] in github_ids
            else tapd_intake.revalidate_request(conn, tapd, ticket, member["request_id"], revision=revision)
        )
        if (
            current is None
            or current.reason != "allowed"
            or current.capability != active["capability"]
            or repo["full_name"] not in current.repos
        ):
            return None
    return active, members, configured_repo


@dataclass(frozen=True, slots=True)
class SourceDispatchObservation:
    """Trusted refresh result materialized outside any SQLite transaction."""

    claim: SourceClaim
    observation: TicketObservation
    publication: PublicationFacts
    mixed: source_github_observation.MixedWorkerProof | None = None


def native_dispatch_binding(conn, task_id, attempt_id):
    """Resolve the prepared attempt and its durable source membership."""
    attempt = source_tasks.attempt(conn, task_id, attempt_id)
    member = result_queries.task_round_binding(conn, task_id)
    if attempt is None or member is None:
        return None
    metadata = _json_object(attempt["metadata_json"])
    manifest = metadata.get("native_result")
    if (
        attempt["status"] != "prepared"
        or not source_tasks.is_latest_attempt(conn, task_id, attempt["attempt_no"])
        or metadata.get("result_contract") != NATIVE_RESULT_CONTRACT
        or not isinstance(manifest, dict)
        or any(manifest.get(key) != member[key] for key in ("round_id", "request_id", "repo_id", "capability"))
    ):
        return None
    return attempt, member, metadata


def worker_observation_target(task_id, attempt_id, facts, observed_at):
    attempt, member, _metadata = facts
    return source_github_observation.WorkerObservationTarget(
        task_id,
        attempt_id,
        context_digest([dict(attempt), dict(member)]),
        observed_at,
    )


def verify_source_dispatch(conn, config, refresh, revision, task_info, repo_id, repo, *, now):
    """Check the precise attempt, source fence, and all frozen input under the writer."""
    facts = native_dispatch_binding(conn, task_info["task_id"], task_info["attempt_id"])
    if facts is None:
        return "native_attempt_unavailable"
    _, member, metadata = facts
    if repo_id != member["repo_id"] or refresh.claim.source_id != member["source_id"]:
        return "native_scope_changed"
    target = (
        worker_observation_target(task_info["task_id"], task_info["attempt_id"], facts, refresh.observation.observed_at)
        if refresh.mixed is not None
        else None
    )
    authorized = native_round_authority(
        conn,
        config,
        refresh.claim,
        round_id=member["round_id"],
        revision=revision,
        repo_id=repo_id,
        capability=member["capability"],
        now=now,
        task_id=task_info["task_id"],
        mixed_proof=refresh.mixed,
        target=target,
    )
    if not authorized:
        return "native_authorization_unavailable"
    if any(
        repo.get(field) != authorized[2].get(field)
        for field in (
            "full_name",
            "repo_root",
            "worktree_root",
            "default_base_branch",
            "github_account",
        )
    ):
        return "native_scope_changed"
    if refresh.mixed is None:
        freshness = tapd_intake.round_input_status(conn, authorized[0], revision=revision)
        if freshness == "changed":
            return "native_input_changed"
        if freshness not in {"current", "frozen"}:
            return "native_authorization_unavailable"
    purpose = metadata.get("native_workspace") or {}
    expected = metadata.get("native_pr_selection")
    if (
        expected is not None
        or purpose.get("pr_selection_required") is True
        or "native_route_resolution" in metadata
        and member["route_id"] in {"new-pr", "update-existing-pr"}
    ):
        if (
            not isinstance(expected, str)
            or purpose.get("pr_selection_required") is not True
            or purpose.get("pr_selection") != expected
        ):
            return "native_pr_selection_changed"
        decision = source_pr_selection.bound_decision(
            conn,
            source_id=member["source_id"],
            round_id=member["round_id"],
            repo_id=repo_id,
            fingerprint=expected,
        )
        if decision is None or any(
            decision.get(recorded) != authorized[2].get(configured)
            for recorded, configured in (
                ("repository", "full_name"),
                ("publisher", "github_account"),
                ("default_base_branch", "default_base_branch"),
            )
        ):
            return "native_pr_selection_changed"
    # Route override fields may select a Worker but cannot widen the action contract.
    settings, rejection = result_validation.context(
        conn,
        task_info["task_id"],
        task_info["attempt_id"],
        include_scope=False,
        allow_completed=False,
        native_launch_preflight=True,
    )
    if rejection or settings["native"] is None:
        return "native_binding_changed"
    return None
