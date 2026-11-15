"""Scope native publication selection and recover local root completion."""

import os
from collections import Counter
from collections.abc import Mapping
from contextlib import closing, contextmanager
from datetime import timedelta

from robert_agent.adapters.tapd.client import TapdClient, TapdError
from robert_agent.delivery.application import source_completion, source_delivery_observation, source_pr_lineage
from robert_agent.delivery.lock import source_publication_lock
from robert_agent.foundation import values
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import source_members, source_publication, source_results
from robert_agent.persistence.stores import source_coordination, source_sessions
from robert_agent.persistence.stores import source_publication as store
from robert_agent.work import source_attention
from robert_agent.work.application import source_github_observation, source_observation
from robert_agent.work.tapd_authority import TicketIdentity

MAX_NATIVE_PAGE = 100
COMPLETION_LIMIT = 20


def _selection(config, *, owner, repo_id, task_ids, action_ids):
    tapd = config.get("tapd") if isinstance(config, Mapping) else None
    if (
        not isinstance(tapd, Mapping)
        or tapd.get("enabled") is not True
        or not isinstance(owner, str)
        or not owner.strip()
        or len(owner) > 512
    ):
        return None
    host = tapd.get("host")
    projects = tapd.get("projects", ())
    repos = config.get("repos", ())
    if (
        not isinstance(host, str)
        or not host
        or not isinstance(projects, (list, tuple))
        or not isinstance(repos, (list, tuple))
        or any(
            not isinstance(project, Mapping) or not isinstance(project.get("workspace_id"), str) for project in projects
        )
        or any(not isinstance(repo, Mapping) or not isinstance(repo.get("full_name"), str) for repo in repos)
    ):
        return None
    workspaces = Counter(project["workspace_id"] for project in projects)
    scopes = tuple((host, workspace) for workspace, count in workspaces.items() if count == 1)
    if not scopes:
        return None
    repo_names = Counter(repo["full_name"] for repo in repos)
    enabled = {
        repo["full_name"] for repo in repos if repo.get("enabled", True) is True and repo_names[repo["full_name"]] == 1
    }
    repositories = tuple(
        (project["workspace_id"], name)
        for project in projects
        for name in (project.get("repositories") or ())
        if isinstance(name, str) and name in enabled
    )
    return {
        "scopes": scopes,
        "repositories": repositories,
        "owner": owner,
        "now": values.utc_now(),
        "repo_id": repo_id,
        "task_ids": task_ids,
        "action_ids": action_ids,
    }


def source_publication_page(
    conn,
    config,
    *,
    source_owner,
    limit,
    repo_id=None,
    task_ids=None,
    action_ids=None,
    tapd_observation_provider=None,
    tapd_identity_provider=None,
    provider=None,
    reconcile_only=False,
    github_allowed=True,
    recover=True,
):
    """Read a bounded effect page and an independent bounded local recovery page."""
    scope = _selection(config, owner=source_owner, repo_id=repo_id, task_ids=task_ids, action_ids=action_ids)
    if scope is None:
        return (), ()
    observe = callable(tapd_observation_provider)
    read_tapd = provider != "github" and observe
    read_github = provider != "tapd" and github_allowed
    repo_names = Counter(item["full_name"] for item in config.get("repos", ()))
    github_repositories = tuple(
        item["full_name"]
        for item in config.get("repos", ())
        if item.get("enabled", True) is True
        and repo_names[item["full_name"]] == 1
        and isinstance(item.get("github_account"), str)
        and item["github_account"]
    )
    actions = (
        source_publication.ready_actions(
            conn,
            **scope,
            limit=min(limit, MAX_NATIVE_PAGE),
            read_tapd=read_tapd,
            read_github=read_github,
            may_write_tapd=not reconcile_only and callable(tapd_identity_provider),
            may_write_github=not reconcile_only and observe,
            github_repositories=github_repositories,
        )
        if limit > 0 and (read_tapd or read_github)
        else ()
    )
    completions = (
        source_results.completion_candidates(
            conn,
            **scope,
            limit=COMPLETION_LIMIT,
            include_mixed=observe and not reconcile_only,
        )
        if recover
        else ()
    )
    return actions, completions


def source_publication_ready(
    conn,
    config,
    *,
    source_owner,
    repo_id=None,
    task_ids=None,
    action_ids=None,
    tapd_observation_provider=None,
    tapd_identity_provider=None,
    provider=None,
    reconcile_only=False,
    github_allowed=True,
    recover=True,
):
    """Ask whether a scoped native effect or local completion is due without contacting TAPD."""
    actions, completions = source_publication_page(
        conn,
        config,
        source_owner=source_owner,
        limit=1,
        repo_id=repo_id,
        task_ids=task_ids,
        action_ids=action_ids,
        tapd_observation_provider=tapd_observation_provider,
        tapd_identity_provider=tapd_identity_provider,
        provider=provider,
        reconcile_only=reconcile_only,
        github_allowed=github_allowed,
        recover=recover,
    )
    return bool(actions or completions)


@contextmanager
def _writer(db_path):
    with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
        transactions.begin_immediate(conn)
        yield conn


def _pace_completion(conn, claim, candidate, outcome, now):
    if outcome.status in {"held", "ineligible"}:
        source_coordination.save(
            conn,
            claim,
            now=now,
            next_due_at=(utc_timestamp(now) + timedelta(minutes=2)).isoformat(),
            status="held",
            reason=outcome.reason,
            round_id=candidate["round_id"],
        )
    return outcome


def _observe_completion(db_path, config, candidate, claim, scope, provider, client_factory, runner, stop):
    if stop is not None and stop.is_set():
        return None
    try:
        ticket = TicketIdentity(
            candidate["host"],
            candidate["project_key"],
            candidate["source_type"],
            candidate["external_id"],
        )
        tapd = config["tapd"]
        (project,) = (item for item in tapd["projects"] if item["workspace_id"] == ticket.workspace_id)
        if ticket.source_id != claim.source_id or ticket.host != tapd["host"]:
            return None
        client = (client_factory or TapdClient)(executable=tapd["executable"], cwd=tapd["cwd"], environment=os.environ)
        snapshot = source_observation.read_ticket_snapshot(client, ticket, project, provider, stop=stop)
        return source_delivery_observation.observe_completion(
            db_path,
            config,
            ticket,
            claim,
            scope=scope,
            snapshot=snapshot,
            runner=runner,
            stop=stop,
        )
    except ControlDeadlineExceeded:
        raise
    except (OSError, TapdError, TypeError, ValueError, KeyError, AttributeError):
        return None


def recover_source_completion(
    db_path,
    config,
    candidate,
    *,
    source_owner,
    repo_id=None,
    task_ids=None,
    action_ids=None,
    tapd_client_factory=None,
    tapd_observation_provider=None,
    github_discussion_runner=None,
    stop=None,
    reconcile_only=False,
):
    """Retain the source publication lock while reading mixed authority outside the writer."""
    source_id, root_id = candidate["source_id"], candidate["source_result_id"]
    may_observe = callable(tapd_observation_provider) and not reconcile_only
    with source_publication_lock(db_path, source_id) as acquired:
        if not acquired:
            return source_completion.CompletionOutcome("held", "source_publication_locked")
        with _writer(db_path) as conn:
            scope = _selection(config, owner=source_owner, repo_id=repo_id, task_ids=task_ids, action_ids=action_ids)
            candidates = (
                source_results.completion_candidates(
                    conn,
                    **scope,
                    limit=1,
                    source_result_id=root_id,
                    include_mixed=may_observe,
                )
                if scope is not None
                else ()
            )
            if not candidates or candidates[0]["source_id"] != source_id:
                return source_completion.CompletionOutcome("ineligible", "source_completion_not_due")
            candidate = candidates[0]
            now = scope["now"]
            mixed = bool(source_members.round_github_request_ids(conn, candidate["round_id"]))
            held = source_sessions.acquire_for_owner(
                conn,
                source_id=source_id,
                owner=source_owner,
                now=now,
                lease_seconds=180 if mixed else 60,
            )
            if held is None:
                return source_completion.CompletionOutcome("held", "source_fence_unavailable")
            if source_results.completion_candidates(
                conn, **scope, limit=1, source_result_id=root_id, linkage_only=True
            ):
                linked = source_pr_lineage.ensure_root_pr_lineage(conn, held, source_result_id=root_id, now=now)
                if not linked or not source_results.completion_candidates(
                    conn,
                    **scope,
                    limit=1,
                    source_result_id=root_id,
                    include_mixed=may_observe,
                ):
                    outcome = source_completion.CompletionOutcome(
                        "lineage_recovered" if linked else "held",
                        "source_pr_lineage_recovered" if linked else "source_pr_lineage_unavailable",
                    )
                    return _pace_completion(conn, held, candidate, outcome, now)
            if not mixed:
                outcome = source_completion.complete_source_result(
                    conn,
                    config,
                    held,
                    source_result_id=root_id,
                    now=now,
                )
                return _pace_completion(conn, held, candidate, outcome, now)
            if not may_observe:
                return source_completion.CompletionOutcome("held", "source_completion_observation_unavailable")

            accepted = source_github_observation.accepted_completion_scope(conn, root_id)
            if accepted is None:
                outcome = source_completion.CompletionOutcome("held", "required_receipts_unavailable")
                return _pace_completion(conn, held, candidate, outcome, now)

        observation = _observe_completion(
            db_path,
            config,
            candidate,
            held,
            accepted,
            tapd_observation_provider,
            tapd_client_factory,
            github_discussion_runner,
            stop,
        )
        if stop is not None and stop.is_set():
            return source_completion.CompletionOutcome("held", "source_completion_stopped")
        with _writer(db_path) as conn:
            now = values.utc_now()
            outcome = source_completion.complete_source_result(
                conn,
                config,
                held,
                source_result_id=root_id,
                now=now,
                completion_observation=observation,
            )
            return _pace_completion(conn, held, candidate, outcome, now)


def pace_action(db_path, claim, root_id, action_id, outcome, *, provider, stop):
    # A stale page or a stopped caller must never extend a current retry window.
    if (
        (stop is not None and stop.is_set())
        or outcome.status not in {"deferred", "unknown", "invalid"}
        or outcome.reason
        in {
            "publication_backoff",
            "source_fence_unavailable",
            "source_fence_or_scope_lost",
            "stopped",
        }
    ):
        return
    with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
        transactions.begin_immediate(conn)
        now = values.utc_now()
        if not source_sessions.lock_claim(conn, claim, now=now):
            return
        if outcome.status == "unknown" or outcome.reason == "saved_native_request_unavailable":
            source_attention.observe_publication_unknown(
                conn,
                claim,
                root_id=root_id,
                action_id=action_id,
                provider=provider,
                reason=outcome.reason,
                now=now,
            )
        retryable = outcome.status != "invalid"
        next_at = (utc_timestamp(now) + timedelta(minutes=1 if outcome.status == "unknown" else 2)).isoformat()
        store.save_action_pacing(
            conn,
            source_id=claim.source_id,
            source_result_id=root_id,
            action_id=action_id,
            provider=provider,
            fields={
                "status": "attention" if outcome.status in {"unknown", "invalid"} else "deferred",
                "reason_code": outcome.reason,
                "retryable": retryable,
                "next_retry_at": next_at if retryable else None,
                "failed_at": now,
            },
        )
