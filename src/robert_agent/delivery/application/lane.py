"""Compose native publication and the GitHub delivery budget."""

from contextlib import closing, contextmanager
import os
import subprocess

from robert_agent.adapters.github import budget as github_budget
from robert_agent.adapters.github import read as github_read
from robert_agent.adapters.notifications import wakeup as wakeup_notifier
from robert_agent.configuration import command as validate_config
from robert_agent.delivery.application import notifications as notify
from robert_agent.delivery.application import publish as publisher
from robert_agent.delivery.application import source_publication_queue
from robert_agent.execution import source_controller
from robert_agent.execution.application import cutover as result_cutover
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.persistence import database as db_database, records as db_records
from robert_agent.persistence.queries import repos as db_read_repos

_REPO_LIMIT = 5
_SOURCE_LIMIT = 20
_SOURCE_WINDOW = 120


def source_ready(
    conn,
    config,
    *,
    source_owner,
    repo_id=None,
    provider="tapd",
    reconcile_only=False,
    recover=True,
    github_allowed=True,
    tapd_observation_provider=None,
    tapd_identity_provider=None,
):
    return source_publication_queue.source_publication_ready(
        conn,
        config,
        source_owner=source_owner,
        repo_id=repo_id,
        provider=provider,
        reconcile_only=reconcile_only,
        recover=recover,
        github_allowed=github_allowed,
        tapd_observation_provider=tapd_observation_provider,
        tapd_identity_provider=tapd_identity_provider,
    )


@contextmanager
def _controller(db_path, config, stop, *, readiness):
    if (config.get("tapd") or {}).get("enabled") is not True:
        yield None, stop, "disabled"
        return
    if stop is not None and stop.is_set():
        yield None, stop, "source_publication_stopped"
        return
    if not os.environ.get("_ROBERT_EXECUTOR_ID"):
        with closing(db_database.connect(db_path)) as conn:
            ready = readiness(conn, source_controller.standalone_owner(db_path))
        if not ready:
            yield None, stop, "idle"
            return
    with source_controller.publication(db_path, config.get("config_path"), seconds=_SOURCE_WINDOW, stop=stop) as owned:
        owner, guard, status = owned
        if owner is None:
            yield owned
            return
        with closing(db_database.connect(db_path)) as conn:
            ready = readiness(conn, owner)
        if guard.is_set():
            yield None, guard, "source_publication_stopped"
        elif ready:
            yield owned
        else:
            yield None, stop, "idle"


def publish_github_repo(
    db_path,
    config,
    repo_id,
    *,
    limit=None,
    include_legacy=True,
    reconcile_only=False,
    tapd_client_factory=None,
    tapd_observation_provider=None,
    stop=None,
    github_budget_check=None,
):
    """Merge one configured repository's native and legacy effects under its budget."""

    def ready(conn, owner):
        return limit != 0 and source_ready(
            conn,
            config,
            source_owner=owner,
            repo_id=repo_id,
            provider="github",
            recover=False,
            reconcile_only=reconcile_only,
            tapd_observation_provider=tapd_observation_provider,
        )

    with _controller(db_path, config, stop, readiness=ready) as (owner, guard, status):
        if status in {"source_publication_stopped", "control_owner_required", "control_child_attention"}:
            return {"ok": False, "status": status, "failure_scope": "repository", "pending_count": 0}
        if not owner and not include_legacy:
            return {"ok": True, "status": status, "pending_count": 0}
        options = {"stop": stop}
        if limit is not None:
            options["limit"] = limit
        if owner:
            options.update(
                source_owner=owner,
                stop=guard,
                source_reconcile_only=reconcile_only,
                tapd_client_factory=tapd_client_factory,
                tapd_observation_provider=tapd_observation_provider,
                github_budget_check=github_budget_check
                or publisher.github_publication_budget(db_path, config, stop=guard),
            )
        return publisher.publish_ready_actions(
            db_path,
            provider="github",
            config=config,
            repo_id=repo_id,
            recover_sources=False,
            include_legacy=include_legacy,
            **options,
        )


def publish_sources(
    db_path,
    config,
    *,
    limit=None,
    local_only=False,
    tapd_client_factory=None,
    tapd_observation_provider=None,
    tapd_identity_provider=None,
    stop=None,
):
    """Run one project-scoped native pass with the canonical controller owner."""
    if (config.get("tapd") or {}).get("enabled") is not True:
        return {}
    if local_only or limit == 0:
        tapd_client_factory = tapd_observation_provider = tapd_identity_provider = None
    options = {
        "tapd_observation_provider": tapd_observation_provider,
        "tapd_identity_provider": tapd_identity_provider,
    }

    def ready(conn, owner):
        return source_ready(conn, config, source_owner=owner, **options)

    with _controller(db_path, config, stop, readiness=ready) as (owner, guard, status):
        if owner is None:
            if status in {"idle", "disabled"}:
                return {}
            return {"ok": status == "skipped_active_daemon", "status": status, "pending_count": 0}
        return publisher.publish_ready_actions(
            db_path,
            provider="tapd",
            source_owner=owner,
            limit=_SOURCE_LIMIT if limit is None else limit,
            config=config,
            tapd_client_factory=tapd_client_factory,
            stop=guard,
            **options,
        )


def add_publish_counts(target, result):
    """Combine an independent provider pass into the caller's existing budget record."""
    for name, count in result.items():
        if name.endswith("_count") and type(count) is int:
            target[name] = target.get(name, 0) + count


def publish(
    config_path,
    *,
    dry_run=False,
    limit=None,
    source_only=False,
    native_only=False,
    source_reconcile_only=False,
    tapd_client_factory=None,
    tapd_observation_provider=None,
    tapd_identity_provider=None,
    stop=None,
):
    config = validate_config.validate_config(config_path, skip_external=True)
    if not config["ok"]:
        return config
    db_path = config["db_path"]
    cutover = result_cutover.check(db_path) if dry_run else result_cutover.prepare(db_path)
    if not cutover["ok"]:
        return cutover
    with closing(db_database.connect(db_path)) as conn:
        repos = [
            (row[0], repo)
            for repo in config["repos"]
            if (row := db_records.first(db_read_repos.find_repo_id_by_full_name(conn, repo["full_name"])))
        ]
    repo_ids = [repo_id for repo_id, _ in repos]
    budget = max(_REPO_LIMIT, len(repo_ids) * _REPO_LIMIT) if limit is None else max(0, limit)
    native = (
        publish_sources(
            db_path,
            config,
            limit=budget,
            stop=stop,
            tapd_client_factory=tapd_client_factory,
            tapd_observation_provider=tapd_observation_provider,
            tapd_identity_provider=None if source_reconcile_only else tapd_identity_provider,
        )
        if not dry_run
        else {}
    )
    if source_only:
        return native or {"ok": True, "status": "dry_run" if dry_run else "idle", "pending_count": 0}
    if native.get("status") in {"control_owner_required", "control_child_attention", "source_publication_stopped"}:
        return native
    counts = {}
    add_publish_counts(counts, native)
    extra = {"source_publication": native, **counts} if native else {}
    remaining = budget - counts.get("pending_count", 0)
    native_enabled = config.get("tapd", {}).get("enabled") is True
    legacy_host = os.environ.get("GH_HOST") or "github.com"
    repo_scoped_budget = native_enabled and bool(repos)
    legacy_probe = (
        publisher.github_publication_budget(db_path, config, host=legacy_host, stop=stop)
        if repo_scoped_budget and not (native_only or dry_run) and (limit is None or limit > 0)
        else None
    )
    if not dry_run:
        if stop is not None and stop.is_set():
            return {**extra, "ok": False, "status": "child_stopped"}
        if not native_only:
            notify.deliver_pending(db_path, repo_ids=repo_ids, config=config)
        if config["repos"] and not native_only and not repo_scoped_budget and (limit is None or limit > 0):
            session = github_read.GitHubReadSession(
                db_path,
                account=config["repos"][0]["github_account"],
                host=os.environ.get("GH_HOST", "github.com"),
            )
            try:
                if not github_budget.budget(session, config):
                    return {
                        **extra,
                        "ok": False,
                        "status": "core_rate_limit_floor",
                        "retry_after_seconds": session.poll_interval_seconds or 60,
                    }
            except ControlDeadlineExceeded:
                raise
            except (subprocess.SubprocessError, OSError, ValueError):
                return {**extra, "ok": False, "status": "rate_limit_check_failed", "failure_scope": "repository"}
    results = []
    for repo_id, repo in repos:
        if stop is not None and stop.is_set():
            break
        if not dry_run and native_enabled:
            refusal = legacy_probe(repo["github_account"]) if legacy_probe is not None else None
            if refusal:
                results.append(refusal)
                if legacy_host == "github.com":
                    continue
            if remaining <= 0:
                continue
            result = publish_github_repo(
                db_path,
                config,
                repo_id,
                limit=min(_REPO_LIMIT, remaining),
                include_legacy=not native_only and not refusal,
                reconcile_only=source_reconcile_only,
                tapd_client_factory=tapd_client_factory,
                tapd_observation_provider=tapd_observation_provider,
                stop=stop,
                github_budget_check=legacy_probe if legacy_host == "github.com" else None,
            )
        elif native_only or remaining <= 0:
            break
        else:
            result = publisher.publish_ready_actions(
                db_path,
                repo_id=repo_id,
                dry_run=dry_run,
                limit=min(_REPO_LIMIT, remaining),
                config=config,
                provider="github",
                stop=stop,
            )
        results.append(result)
        add_publish_counts(counts, result)
        remaining -= result.get("pending_count", 0)
    wakeup_notifier.notify(db_path)
    repository_failure = next(
        (result for result in results if not result.get("ok") and result.get("failure_scope") == "repository"),
        None,
    )
    if repository_failure:
        return {
            **extra,
            **counts,
            **{
                key: repository_failure[key]
                for key in ("ok", "status", "failure_scope", "retry_after_seconds")
                if key in repository_failure
            },
        }
    ok = native.get("ok", True) and all(result.get("ok") for result in results)
    return {
        **extra,
        **counts,
        "ok": ok,
        "status": "published",
        "repo_count": len(results),
        "failure_scope": None if ok else "task",
    }
