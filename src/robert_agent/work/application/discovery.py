"""Collect, authorize and order one repository's discovery inputs."""

from robert_agent.execution.application import replay as execution_replay
from robert_agent.adapters.github import discovery as discover
from robert_agent.adapters.github import sources as github_sources
from robert_agent.foundation import errors as run_errors
from robert_agent.execution import journal as run_journal
from robert_agent.work.application import github_requests
from robert_agent.work.application import participants
from robert_agent.configuration import routes as route_config
from robert_agent.foundation import values
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.security import authorization as authorize
from robert_agent.work import queries as work_queries
import json
import subprocess


def discover_requests(
    conn,
    *,
    repo,
    repo_id,
    run_id,
    run_now,
    config_result,
    fixture_path,
    skip_external,
    observation,
    discovery_runner,
    notification_hints,
    system_decisions,
    summary,
):
    known_workstreams = work_queries.load_known_workstreams(conn, repo_id)
    review_participants = participants.load_review_participants(conn, repo_id)
    active_workstreams = work_queries.load_active_workstreams(conn, repo_id)
    run_journal.mark_repo_run_step(conn, run_id, repo_id, "discover", "running", run_now)
    conn.commit()  # Discovery can wait on remote I/O; never pin the domain writer.
    if observation is not None:
        raw_events = []
        for raw in observation["raw_events"]:
            latest_state = github_sources.read_source_state(
                repo["full_name"],
                raw.get("source_type", "issue"),
                raw["number"],
                discovery_runner,
            )
            if latest_state is None:
                summary.update(
                    status="failed",
                    failure_status="observation_unavailable",
                    safe_error="Remote source state could not be verified",
                )
                continue
            raw_events.append({**raw, **latest_state} if latest_state else raw)
        if summary["failure_status"] == "observation_unavailable" and not raw_events:
            error = {
                "status": summary["failure_status"],
                "safe_error": summary["safe_error"],
            }
            run_journal.mark_repo_run_step(conn, run_id, repo_id, "discover", "failed", run_now, error=error)
            for step in ("authorize", "route"):
                run_journal.mark_repo_run_step(
                    conn,
                    run_id,
                    repo_id,
                    step,
                    "skipped",
                    run_now,
                    output={"reason": summary["failure_status"]},
                )
            return None
    elif fixture_path:
        raw_events = discover.load_fixture(fixture_path)
    elif skip_external:
        raw_events = []
    else:
        try:
            discovery_kwargs = {"known_workstreams": known_workstreams}
            if config_result is not None:
                review_settings = route_config.resolve_route_config(
                    config_result, repo, route_config.ROUTE_POLICIES["review-pr"]
                ).get("multi_agent_review", {})
                if review_settings.get("enabled") and len(review_settings.get("workers", [])) > 1:
                    discovery_kwargs["review_author_allowlist"] = review_settings["author_allowlist"]
            if notification_hints is not None:
                discovery_kwargs["notification_hints"] = notification_hints
                discovery_kwargs["include_notifications"] = False
            if discovery_runner is not None:
                discovery_kwargs["runner"] = discovery_runner
            raw_events = discover.collect_live_events(repo, **discovery_kwargs)
        except ControlDeadlineExceeded:
            raise
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            FileNotFoundError,
            json.JSONDecodeError,
        ) as exc:
            error = run_errors.safe_discovery_error(exc)
            summary["status"] = "failed"
            summary["failure_status"] = error["status"]
            summary["safe_error"] = error["safe_error"]
            run_journal.mark_repo_run_step(
                conn,
                run_id,
                repo_id,
                "discover",
                "failed",
                values.utc_now(),
                error=error,
            )
            for step in ["authorize", "route"]:
                run_journal.mark_repo_run_step(
                    conn,
                    run_id,
                    repo_id,
                    step,
                    "skipped",
                    run_now,
                    output={"reason": error["status"]},
                )
            return None
    summary["raw_event_count"] = len(raw_events)
    run_journal.mark_repo_run_step(
        conn,
        run_id,
        repo_id,
        "discover",
        "failed" if summary["failure_status"] == "observation_unavailable" else "succeeded",
        run_now,
        output={"raw_event_count": len(raw_events)},
        error={"status": summary["failure_status"], "safe_error": summary["safe_error"]}
        if summary["failure_status"] == "observation_unavailable"
        else None,
    )

    run_journal.mark_repo_run_step(conn, run_id, repo_id, "authorize", "running", run_now)
    try:
        normalized = discover.normalize_events(raw_events, repo)
        normalized = participants.attach_review_participants(normalized, review_participants)
    except (KeyError, TypeError, ValueError) as exc:
        error = run_errors.safe_discovery_error(exc)
        summary["status"] = "failed"
        summary["failure_status"] = error["status"]
        summary["safe_error"] = error["safe_error"]
        run_journal.mark_repo_run_step(
            conn,
            run_id,
            repo_id,
            "discover",
            "failed",
            values.utc_now(),
            error=error,
        )
        for step in ["authorize", "route"]:
            run_journal.mark_repo_run_step(
                conn,
                run_id,
                repo_id,
                step,
                "skipped",
                run_now,
                output={"reason": error["status"]},
            )
        return None
    decisions = authorize.authorize_events(
        normalized,
        repo,
        known_workstreams=known_workstreams,
    )
    # Native review requests create new rounds, including for known PRs.
    # Reuse the original requester/target authorization without treating
    # this explicit request as ordinary follow-up comment context.
    if config_result is not None:
        for index, event in enumerate(decisions):
            if event.get("source_type") == "pull_request" and event.get("event_type") == "review_request":
                candidate = route_config.resolve_route_config(
                    config_result, repo, route_config.ROUTE_POLICIES["review-pr"]
                )
                if len(route_config.review_workers_for_author(candidate, event.get("pr_author_login"))) > 1:
                    requested = {
                        **event,
                        "actor_login": event.get("requester_login") or event.get("actor_login"),
                    }
                    decisions[index] = authorize.authorize_event(requested, repo, known_workstreams=set())
    decisions.extend(system_decisions if not fixture_path and (not skip_external or observation is not None) else [])
    decisions.extend(
        execution_replay.decisions(
            conn,
            repo_id,
            run_now,
            system_decisions if not fixture_path and (not skip_external or observation is not None) else [],
            repo=repo,
        )
    )
    unique_decisions = {}
    for event in decisions:
        fingerprint = event["event_fingerprint"]
        previous = unique_decisions.get(fingerprint)
        unique_decisions[fingerprint] = discover.merge_event_body(previous, event) if previous else event
    decisions = list(unique_decisions.values())
    decisions = sorted(decisions, key=github_requests.event_priority)
    summary["decision_count"] = len(decisions)
    run_journal.mark_repo_run_step(
        conn,
        run_id,
        repo_id,
        "authorize",
        "succeeded",
        run_now,
        output={"decision_count": len(decisions)},
    )
    return decisions, known_workstreams, active_workstreams
