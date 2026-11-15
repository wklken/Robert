"""Read exact source/event ownership and independently proved native PR lineage."""

from dataclasses import dataclass

from sqlalchemy import and_, func, select

from robert_agent.foundation.contracts.delivery import GitHubActionType, PublishStatus
from robert_agent.foundation.contracts.work import RequestEventState, SourceAuthorizationType, SourceIngressPhase
from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries import source_pr_lineage, sqlite_master
from robert_agent.persistence.rounds.source_evidence import json_object


@dataclass(frozen=True, slots=True)
class Association:
    tapd_source_id: str | None
    pr_workstream_id: str | None
    lineage: source_pr_lineage.ConfirmedPRLineage | None


def event(conn, event_id):
    source_event = tables["source_events"]
    return records.first(statements.read(conn, select(source_event).where(source_event.c.event_id == event_id)))


def source(conn, source_id):
    table = tables["sources"]
    return records.first(statements.read(conn, select(table).where(table.c.source_id == source_id)))


def for_event(conn, event_id):
    bridge = tables["source_github_ingress"]
    return records.first(statements.read(conn, select(bridge).where(bridge.c.event_id == event_id)))


def for_request(conn, request_id):
    """Read a bridge and its fixed target by the exact admitted request."""
    bridge = tables["source_github_ingress"]
    return records.first(statements.read(conn, select(bridge).where(bridge.c.request_id == request_id)))


def event_already_bound(conn, event_id):
    """Exclude earlier ordinary task or ingress ownership of the same event."""
    tasks, ledger = (tables[name] for name in ("task_events", "request_events"))
    return bool(
        statements.read(conn, select(tasks.c.event_id).where(tasks.c.event_id == event_id).limit(1))
        or statements.read(conn, select(ledger.c.event_id).where(ledger.c.event_id == event_id).limit(1))
    )


def _receipt_candidate(conn, url, repo_id):
    """Find exact locally known creation evidence before workstream adoption."""
    action, link, root, origin, task, ticket = (
        tables[name]
        for name in ("github_actions", "source_required_actions", "source_results", "workstreams", "tasks", "sources")
    )
    return records.first(
        statements.read(
            conn,
            select(root.c.source_id)
            .select_from(
                action.join(link, link.c.action_id == action.c.action_id)
                .join(root, root.c.source_result_id == link.c.source_result_id)
                .join(task, task.c.task_id == action.c.task_id)
                .join(
                    origin,
                    (origin.c.workstream_id == task.c.workstream_id) & (origin.c.primary_source_id == root.c.source_id),
                )
                .join(ticket, ticket.c.source_id == root.c.source_id)
            )
            .where(
                action.c.provider == "github",
                action.c.action_type == GitHubActionType.OPEN_PR.value,
                action.c.publish_status == PublishStatus.PUBLISHED.value,
                func.lower(action.c.target_url) == url.casefold(),
                origin.c.repo_id == repo_id,
                ticket.c.provider == "tapd",
            )
            .limit(1),
        )
    )


def association(conn, pr_source, repo_id):
    """Return a verified association, a held evidence gap, or an ordinary PR."""
    if (
        pr_source is None
        or pr_source["provider"] != "github"
        or pr_source["source_type"] != "pull_request"
        or pr_source["host"] != "github.com"
        or pr_source["repo_id"] != repo_id
    ):
        return None
    candidates = []
    stream_table, links = (tables[name] for name in ("workstreams", "workstream_sources"))
    streams = statements.read(
        conn,
        select(stream_table)
        .where(
            (stream_table.c.primary_source_id == pr_source["source_id"])
            | select(links.c.source_id)
            .where(
                links.c.source_id == pr_source["source_id"],
                links.c.workstream_id == stream_table.c.workstream_id,
                links.c.relationship.in_(("primary", "derived_pr")),
            )
            .exists()
        )
        .limit(2),
    )
    for stream in streams:
        metadata = json_object(stream["metadata_json"]) or {}
        origin = source_pr_lineage.pr_workstream(conn, stream["origin_workstream_id"])
        tapd = source(conn, origin["primary_source_id"]) if origin else None
        is_tapd = tapd is not None and tapd["provider"] == "tapd"
        if not is_tapd and "tapd_creation" not in metadata:
            continue
        source_id = tapd["source_id"] if is_tapd else None
        creation = metadata.get("tapd_creation")
        proof = None
        if (
            source_id
            and isinstance(creation, dict)
            and all(isinstance(creation.get(key), str) for key in ("action_id", "source_result_id"))
        ):
            proof = source_pr_lineage.confirmed_pr_lineage(
                conn,
                source_id=source_id,
                repo_id=repo_id,
                workstream_id=stream["workstream_id"],
                action_id=creation["action_id"],
            )
        candidates.append(Association(source_id, stream["workstream_id"], proof))
    if candidates:
        if len(candidates) == len(streams) == 1:
            return candidates[0]
        return Association(None, None, None)
    if isinstance(pr_source["html_url"], str):
        receipt = _receipt_candidate(conn, pr_source["html_url"], repo_id)
        if receipt:
            return Association(receipt[0], None, None)
    return None


def pending_creation(conn, repo_id, repository, number):
    return bool(_receipt_candidate(conn, f"https://github.com/{repository}/pull/{number}", repo_id))


def pending_held(source_id):
    """Select pending-ledger bridge members that still need a current probe."""
    bridge, ingress, member, current = (
        tables[name]
        for name in ("source_github_ingress", "ingress_requests", "request_events", "source_github_observations")
    )
    return (
        select(ingress.c.request_id)
        .select_from(
            bridge.join(
                ingress,
                and_(
                    ingress.c.request_id == bridge.c.request_id,
                    ingress.c.source_id == bridge.c.pr_source_id,
                    ingress.c.repo_id == bridge.c.repo_id,
                ),
            )
            .join(member, and_(member.c.request_id == bridge.c.request_id, member.c.event_id == bridge.c.event_id))
            .outerjoin(current, current.c.request_id == bridge.c.request_id)
        )
        .where(
            bridge.c.tapd_source_id == source_id,
            bridge.c.target_json.is_not(None),
            ingress.c.provider == "github",
            ingress.c.auth_type == SourceAuthorizationType.GITHUB_MENTION.value,
            ingress.c.phase.in_(
                (SourceIngressPhase.HELD.value, SourceIngressPhase.PENDING.value, SourceIngressPhase.PAUSED.value)
            ),
            member.c.state == RequestEventState.PENDING.value,
        )
        .order_by(func.coalesce(current.c.observed_at, ""), bridge.c.created_at, bridge.c.event_id)
    )


def pending_held_ids(conn, source_id, *, limit):
    if not sqlite_master.table_exists(conn, "source_github_observations"):
        return ()
    return tuple(row[0] for row in statements.read(conn, pending_held(source_id).limit(limit)))


def current_observation(conn, request_id):
    current = tables["source_github_observations"]
    return records.first(statements.read(conn, select(current).where(current.c.request_id == request_id)))
