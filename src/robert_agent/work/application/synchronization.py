"""application synchronization responsibilities."""

from robert_agent.persistence.queries import github_events as db_read_github_events

from robert_agent.persistence import database as db_database
from robert_agent.persistence import errors as db_errors

from robert_agent.persistence.queries import github_scan_batches as db_read_github_scan_batches
from robert_agent.persistence.queries import wakeups as db_read_wakeups
from robert_agent.persistence.queries import workstreams as db_read_workstreams
from robert_agent.persistence.stores import wakeups as db_write_wakeups
from robert_agent.adapters.github import sources as github_sources
from contextlib import closing
import hashlib
import json
import os
import subprocess
import time
from robert_agent.adapters.github import discovery as discover
from robert_agent.workflows import pr_health
from robert_agent.persistence.schema import database as storage
from robert_agent.configuration import command as validate_config
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.adapters.github import read as github_read
from robert_agent.adapters.github import recording as github_recording
from robert_agent.adapters.github import budget as github_budget
from robert_agent.work import scan_batches as scan_batches

from robert_agent.work import source_policy as source_policy


def review_allowlist(config, repo):
    from robert_agent.configuration import routes as route_config

    if config is None:
        return []
    settings = route_config.resolve_route_config(config, repo, route_config.ROUTE_POLICIES["review-pr"]).get(
        "multi_agent_review", {}
    )
    return (
        sorted(settings.get("author_allowlist") or [])
        if settings.get("enabled") and len(settings.get("workers", [])) > 1
        else []
    )


def context_digest(repo, known_workstreams, *, config=None):
    values = [
        discover.SOURCE_CONTEXT_VERSION,
        repo["full_name"],
        repo["github_account"],
        sorted(repo.get("trusted_actors", [])),
        sorted(known_workstreams),
        review_allowlist(config, repo),
        os.environ.get("GH_HOST", "github.com"),
    ]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def scan_repo(config, repo, session, hints):
    from robert_agent.work import queries as work_queries
    from robert_agent.work import repositories as work_repositories

    db_path = config["db_path"]
    recorded = github_recording.RecordingRunner(session)
    with closing(db_database.connect(db_path, timeout=5)) as conn, conn:
        repo_id = work_repositories.upsert_repo(conn, repo)
        known = work_queries.load_known_workstreams(conn, repo_id)
        health_sources = {(row[0], row[1]) for row in db_read_workstreams.find_for_scan_repo(conn, repo_id)}
        sources = list(
            dict.fromkeys(
                (row[4], row[5], (row[4], row[5]) in health_sources)
                for row in work_queries.remote_source_rows(conn, repo_id)
            )
        )
        pending_rows = db_read_github_scan_batches.find_for_scan_repo(conn, repo_id)
        pending = [(row[2],) for row in pending_rows]
        # A successful batch is not proof that every driving event created a
        # task. Reobserve orphaned requests; never dispatch their saved payload
        # or its historical authorization without fresh source reads.
        trusted = list(repo.get("trusted_actors") or [])
        unlinked = db_read_github_events.scan_repo_filtered(conn, trusted, repo_id, repo["github_account"])
    hints = list(hints)
    recovery_events = [json.loads(encoded) for (encoded,) in unlinked]
    for raw in [
        *(raw for (encoded,) in pending for raw in json.loads(encoded)),
        *(raw for raw in recovery_events if raw.get("drives_execution")),
    ]:
        source = (raw.get("source_type", "issue"), raw["number"])
        if not any((kind, number) == source for kind, number, _ in sources):
            sources.append((*source, False))
        if not any(
            h.get("number") == raw["number"] and h.get("source_type") == raw.get("source_type", "issue") for h in hints
        ):
            hints.append(
                {
                    "id": str(raw.get("id") or raw.get("event_fingerprint")),
                    "repo_full_name": repo["full_name"],
                    "number": raw["number"],
                    "source_type": raw.get("source_type", "issue"),
                    "event_type": "notification",
                    "event_at": raw.get("event_at"),
                }
            )
    remote_states = {}
    context = context_digest(repo, known, config=config)
    try:
        for source_type, number, health_candidate in sources:
            remote_states[(source_type, number)] = github_sources.read_source_state(
                repo["full_name"], source_type, number, recorded
            )
            automation = repo.get("pr_automation") or {}
            if health_candidate and any((automation.get(k) or {}).get("enabled") for k in ("ci", "conflict")):
                snapshot = pr_health.fetch_pr_snapshot(repo["full_name"], number, runner=recorded)
                ci = automation.get("ci") or {}
                if (
                    snapshot
                    and snapshot.get("author_login", "").casefold() == repo["github_account"].casefold()
                    and ci.get("enabled")
                    and snapshot.get("head_sha")
                ):
                    pr_health.collect_ci_observations(
                        repo["full_name"],
                        snapshot["head_sha"],
                        check_allowlist=ci["check_allowlist"],
                        max_summary_chars=ci["max_failure_summary_chars"],
                        runner=recorded,
                    )
        events = session.collect_events(
            repo,
            known_workstreams=known,
            notification_hints=hints,
            include_notifications=False,
            review_author_allowlist=review_allowlist(config, repo),
        )
        # Notification lookup can return only the latest mention (or an older
        # assignment). Read the full discussion for each orphaned source so an
        # earlier unconsumed request cannot be hidden by a newer comment.
        for source_type, number in dict.fromkeys(
            (raw.get("source_type", "issue"), raw["number"]) for raw in recovery_events if raw.get("drives_execution")
        ):
            if remote_states.get((source_type, number)) is None:
                continue
            raw = {"number": number, "source_type": source_type, "event_type": "notification"}
            enriched, state = discover.refresh_source_metadata(raw, repo, session)
            if (state or "").lower() != "open":
                continue
            enriched = discover._enrich_pull_request_metadata(enriched, repo, session)
            discussion = session.source_events(
                repo,
                enriched,
                lambda enriched=enriched: discover._enrich_known_workstream_contexts(enriched, repo, session),
                known_workstreams=known,
                notification_hints=[h for h in hints if h["number"] == number and h["source_type"] == source_type],
            )
            events.extend(event for event in discussion if event.get("event_type") != "notification")
        for raw in [*events, *(raw for (encoded,) in pending for raw in json.loads(encoded))]:
            source = (raw.get("source_type", "issue"), raw["number"])
            if source not in remote_states:
                remote_states[source] = github_sources.read_source_state(repo["full_name"], *source, recorded)
        incomplete = {
            (raw.get("source_type", "issue"), raw["number"])
            for raw in events
            if raw.get("authorization_lookup_complete") is False
        }
        covered = {
            source
            for source, state in remote_states.items()
            if state is not None
            and source not in incomplete
            and discover.source_key(repo["full_name"], *source) not in session.failed_sources
        }
        with closing(db_database.connect(db_path, timeout=5)) as conn, conn:
            for old_id, old_context, encoded, retry_at in pending_rows:
                old_sources = {(raw.get("source_type", "issue"), raw["number"]) for raw in json.loads(encoded)}
                if old_context != context and retry_at <= time.time() and not old_sources <= covered:
                    scan_batches.fail_batch(conn, old_id, "source_refresh_unavailable", time.time())
        events = source_policy.refresh_pending_events(
            events, [raw for (encoded,) in pending for raw in json.loads(encoded)], covered, repo
        )
        # Empty polling is not local work. Tracked or pending sources still
        # need a batch for closure, remediation and current-authority checks.
        if events or sources or pending_rows:
            identifier = scan_batches.save_batch(
                db_path, repo_id, context, events, recorded.responses, complete_sources=covered
            )
        else:
            identifier = None
            with closing(db_database.connect(db_path, timeout=5)) as conn, conn:
                scan_batches.save_reads(conn, repo_id, recorded.responses, time.time())
    except Exception:
        # A later discovery/search failure must not resurrect an older successful
        # safety read. Publish both fresh state and explicit failed-read markers.
        with closing(db_database.connect(db_path, timeout=5)) as conn, conn:
            scan_batches.save_reads(conn, repo_id, recorded.responses, time.time())
            for old_id, old_context, _encoded, retry_at in pending_rows:
                if old_context != context and retry_at <= time.time():
                    scan_batches.fail_batch(conn, old_id, "discovery_refresh_failed", time.time())
        raise
    with closing(db_database.connect(db_path, timeout=5)) as conn, conn:
        db_write_wakeups.update_for_scan_repo(conn, repo_id)
    return identifier


def scan(config_path, *, runner=subprocess.run, refresh_context=False):
    config = validate_config.validate_config(config_path, skip_external=True)
    if not config["ok"]:
        return config
    db_path = config["db_path"]
    storage.init_database(db_path)
    repos = config["repos"]
    if refresh_context:
        with closing(db_database.connect(db_path, timeout=5)) as conn:
            pending = {row[0] for row in db_read_wakeups.find_for_scan(conn)}
        repos = [repo for repo in repos if repo["full_name"] in pending]
    sessions, buckets, failures, batches = {}, {}, [], []
    for repo in repos:
        account = repo["github_account"]
        if account in sessions:
            continue
        session = github_read.GitHubReadSession(
            db_path, account=account, host=os.environ.get("GH_HOST", "github.com"), runner=runner
        )
        sessions[account] = session
        try:
            if not github_budget.budget(session, config):
                failures.append({"account": account, "reason": "core_rate_limit_floor"})
                continue
            account_repos = [item for item in repos if item["github_account"] == account]
            try:
                buckets[account] = (
                    session.cached_notifications(account_repos)
                    if refresh_context
                    else session.collect_notifications(account_repos)
                )
            except ControlDeadlineExceeded:
                raise
            except (subprocess.SubprocessError, OSError, ValueError):
                # Search remains a fallback when notification lookup fails.
                buckets[account] = {}
                failures.append({"account": account, "reason": "notification_read_failed"})
        except ControlDeadlineExceeded:
            raise
        except (subprocess.SubprocessError, OSError, ValueError):
            failures.append({"account": account, "reason": "rate_limit_check_failed"})
    for repo in repos:
        if repo["github_account"] not in buckets:
            continue
        try:
            identifier = scan_repo(
                config,
                repo,
                sessions[repo["github_account"]],
                buckets[repo["github_account"]].get(repo["full_name"], []),
            )
            if identifier is not None:
                batches.append(identifier)
        except ControlDeadlineExceeded:
            raise
        except (subprocess.SubprocessError, OSError, ValueError, db_errors.Error):
            failures.append({"repo": repo["full_name"], "reason": "repo_scan_failed"})
    intervals = [session.poll_interval_seconds for session in sessions.values() if session.poll_interval_seconds]
    return {
        "ok": not failures,
        "status": "scanned",
        "context_refresh": refresh_context,
        "batch_count": len(batches),
        "failures": failures,
        "poll_interval_seconds": max(intervals, default=60),
        "retry_after_seconds": max(intervals, default=0)
        if any(item["reason"] == "core_rate_limit_floor" for item in failures)
        else 0,
        "failure_scope": "repository" if failures else None,
        "github_reads": {account: session.metrics() for account, session in sessions.items()},
    }
