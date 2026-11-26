"""Scan for bounded proposals and preserve distinct evidence revisions."""

from contextlib import closing
from datetime import timedelta
import subprocess
from zoneinfo import ZoneInfo

from robert_agent.adapters.git import proposal_evidence
from robert_agent.adapters.services import supervision_model as model_gateway
from robert_agent.adapters.tapd import client as tapd_client
from robert_agent.context import proposal_prompts
from robert_agent.foundation import commands, values
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import proposals as queries
from robert_agent.persistence.stores import proposals as store
from robert_agent.persistence.stores import model_usage
from robert_agent.security.text import safe_text
from robert_agent.work import proposal_feedback, proposals
from robert_agent.work.application import proposal_sources, web_setup


TIMEZONE = ZoneInfo("Asia/Shanghai")
BATCH_LIMIT = 6
MUTABLE_STATES = frozenset({"evaluated", "pending", "held", "blocked"})
_ROUTING_CONFLICT = "原单据的仓库标签冲突，请在 TAPD 修正后复核。"
_NETWORK_ERRORS = (ValueError, OSError, subprocess.SubprocessError, tapd_client.TapdError)


def scan_slot(now):
    local = utc_timestamp(now).astimezone(TIMEZONE)
    return local.date().isoformat() if local.hour >= 9 else None


def due(conn, config, now):
    if not config.get("proposer", {}).get("enabled"):
        return False
    slot = scan_slot(now)
    return bool(slot and not queries.scan_completed(conn, slot))


def _eligible(previous, fingerprint=None):
    return not previous or (
        previous["status"] in MUTABLE_STATES and (fingerprint is None or previous["fingerprint"] != fingerprint)
    )


def _source_occupied(conn, source, previous, *, scheduled=False):
    if previous and previous.get("investigation_task_id"):
        return queries.formal_work_exists(conn, previous["source_key"])
    if previous and previous["status"] == "blocked":
        binding = queries.work_binding(conn, source["source_key"])
        return bool(binding and binding["active_task_id"]) or (
            scheduled and queries.formal_work_exists(conn, source["source_key"])
        )
    if source["provider"] == "tapd" and not source.get("repository"):
        return queries.known_tapd_source(conn, source["identity"])
    return queries.known_source(conn, [source["source_key"]])


def _blocked_candidates(db_path, config, repos, clients, stats):
    setting = config["proposer"]
    with closing(database.connect(db_path)) as conn:
        rows = queries.current(conn, status="blocked", limit=BATCH_LIMIT)
    for row in rows:
        source = values.json_object(row["source_json"])
        repo = repos.get(source["repository"])
        if not repo:
            continue
        if source["provider"] == "github" and source["repository"] not in setting["github_repositories"]:
            continue
        if source["provider"] == "tapd":
            key = source["project_key"]
            if key not in setting["tapd_projects"]:
                continue
        try:
            if source["provider"] == "tapd" and key not in clients:
                clients[key] = tapd_client.client_for(config["tapd"], config["data_dir"])
            current, _ = proposal_sources.refresh(config, repo, source, clients=clients)
        except commands.ControlDeadlineExceeded:
            raise
        except _NETWORK_ERRORS:
            stats["failed_evaluations"] += 1
            continue
        if current:
            yield current


def _write(conn, setting, source, fingerprint, report, now):
    previous = queries.observation(conn, source["identity"])
    if not _eligible(previous, fingerprint) or _source_occupied(conn, source, previous):
        return False
    revision = (previous["revision"] if previous else 0) + bool(report)
    status = "pending" if report else ("blocked" if revision else "evaluated")
    if previous and previous["status"] == "held" and report:
        status = "held"
    row = {
        "repo_id": values.repository_id(source["repository"]),
        "status": status,
        "version": (previous["version"] if previous else 0) + 1,
        "revision": revision,
        "source_key": source["source_key"],
        "fingerprint": fingerprint,
        "updated_at": now,
        "blocked_reason": "" if report or revision == 0 else "新证据不足，请复核提案。",
    }
    proposal_id = previous["proposal_id"] if previous else values.new_id("p")
    if previous:
        # An evidence rejection retains the displayed last approved draft's repository.
        if not report and revision:
            row.update(repo_id=previous["repo_id"], source_key=previous["source_key"])
        store.revise(conn, proposal_id, previous["version"], **row)
    else:
        store.create(
            conn, {**row, "proposal_id": proposal_id, "source_identity": source["identity"], "created_at": now}
        )
    if report:
        store.record_revision(
            conn,
            {
                "proposal_id": proposal_id,
                "revision": revision,
                "source_json": proposals.encoded(source),
                "report_json": proposals.encoded(report),
                "created_at": now,
            },
        )
        if revision == 1:
            proposal_feedback.record_created(conn, setting, proposal_id, source, now)
    return bool(report)


def evaluate(setting, endpoint, source, commit, excerpts, comments, *, on_usage=None):
    prompt = proposal_prompts.assessment(source, commit)
    evidence = {
        "ticket": {key: source[key] for key in ("title", "description", "description_truncated")},
        "discussion": {key: comments[key] for key in ("items", "truncated")},
        "code": excerpts,
    }
    output = model_gateway.request_json(
        instructions=prompt.text,
        evidence=evidence,
        endpoint=endpoint,
        model=setting["model"],
        timeout=30,
        session_id="robert-proposal-" + proposals.digest([source["identity"], commit])[:32],
        on_usage=on_usage,
    )
    return proposals.validate_assessment(output, commit, excerpts)


def scan(config, *, now=None):
    now = now or values.utc_now()
    setting = config["proposer"]
    if not setting["enabled"] or not scan_slot(now):
        return {"ok": True, "status": "disabled_or_not_due"}
    db_path = config["db_path"]
    web_setup.prepare_repositories(config)
    with closing(database.connect(db_path)) as conn:
        if not due(conn, config, now):
            return {"ok": True, "status": "already_scanned"}
    try:
        endpoint = model_gateway.gateway_endpoint(setting["gateway_metadata"])
    except _NETWORK_ERRORS:
        return {"ok": False, "status": "model_unavailable", "safe_error": "Proposer model gateway is unavailable"}
    until = utc_timestamp(now)
    since = until - timedelta(days=7)
    repos = {repo["full_name"]: repo for repo in config["repos"]}
    stats = {"eligible": 0, "evaluated": 0, "proposed": 0, "failed_scopes": [], "failed_evaluations": 0}
    clients, commits = {}, {}
    sources = (
        *_blocked_candidates(db_path, config, repos, clients, stats),
        *proposal_sources.discover(config, since, until, stats, clients),
    )
    unique = {source["identity"]: source for source in sources}
    stats["eligible"] = len(unique)
    for source in sorted(unique.values(), key=lambda source: (source["created_at"], source["identity"])):
        if stats["evaluated"] >= BATCH_LIMIT:
            break
        with closing(database.connect(db_path)) as conn:
            previous = queries.observation(conn, source["identity"])
            if not _eligible(previous) or _source_occupied(conn, source, previous):
                continue
        name = source["repository"]
        call_id = None
        measured = {}
        try:
            if name not in commits:
                commits[name] = proposal_evidence.pinned_commit(repos[name])
            commit = commits[name]
            comments = proposal_sources.discussion(
                source, clients, config["tapd"], github_account=repos[name]["github_account"]
            )
            source = {**source, "discussion_digest": comments["digest"]}
            fingerprint = proposals.digest([source, commit])
            if not _eligible(previous, fingerprint):
                continue
            stats["evaluated"] += 1
            excerpts = proposal_evidence.excerpts(repos[name], commit, source["title"] + "\n" + source["description"])
            report = None
            if excerpts:
                call_id = values.new_id("proposer-model")
                report = evaluate(setting, endpoint, source, commit, excerpts, comments, on_usage=measured.update)
        except commands.ControlDeadlineExceeded:
            raise
        except _NETWORK_ERRORS:
            stats["failed_evaluations"] += 1
            continue
        finally:
            if call_id:
                scope = values.repository_id(name) + "/" + source["identity"]
                with closing(database.connect(db_path)) as conn, conn:
                    model_usage.record(conn, "proposer", call_id, scope, now, measured)
        with closing(database.connect(db_path)) as conn, conn:
            transactions.begin_immediate(conn)
            stats["proposed"] += _write(conn, setting, source, fingerprint, report, now)
    with closing(database.connect(db_path)) as conn, conn:
        store.complete_scan(conn, scan_slot(now), now)
    return {
        "ok": True,
        "status": "partial" if stats["failed_scopes"] or stats["failed_evaluations"] else "completed",
        **stats,
    }


def _daily_stats():
    return {
        "ok": True,
        "status": "completed",
        "discovered": 0,
        "evaluated": 0,
        "updated": 0,
        "expired": 0,
        "failed_scopes": [],
        "failed_evaluations": 0,
    }


def _daily_revision(conn, source, now, *, previous=None, report=None, fingerprint=""):
    revision = previous["revision"] + 1 if previous else 1
    proposal_id = previous["proposal_id"] if previous else values.new_id("p")
    repository = source.get("repository")
    blocked_reason = _ROUTING_CONFLICT if source.get("routing_conflict") else ""
    status = "held" if previous and previous["status"] == "held" else "pending"
    row = {
        "repo_id": values.repository_id(repository) if repository else None,
        "status": "blocked" if blocked_reason else status,
        "version": previous["version"] + 1 if previous else 1,
        "revision": revision,
        "source_key": source["source_key"],
        "fingerprint": fingerprint,
        "updated_at": now,
        "blocked_reason": blocked_reason,
    }
    if previous:
        store.revise(conn, proposal_id, previous["version"], **row)
    else:
        store.create(
            conn, {**row, "proposal_id": proposal_id, "source_identity": source["identity"], "created_at": now}
        )
    store.record_revision(
        conn,
        {
            "proposal_id": proposal_id,
            "revision": revision,
            "source_json": proposals.encoded(source),
            "report_json": proposals.encoded(report or {"feasibility": "unassessed", "recommendation": "low"}),
            "created_at": now,
        },
    )


def _remember_daily(conn, sources, now, stats):
    transactions.begin_immediate(conn)
    for source in sources:
        previous = queries.observation(conn, source["identity"])
        if previous or _source_occupied(conn, source, None):
            continue
        _daily_revision(conn, source, now)
        stats["discovered"] += 1
    for row in queries.expired_holds(conn, now):
        store.revise(
            conn,
            row["proposal_id"],
            row["version"],
            status="pending",
            version=row["version"] + 1,
            defer_until=None,
            updated_at=now,
        )
        stats["expired"] += 1


def _daily_queue(conn, limit, today):
    rows = queries.daily_candidates(conn)

    def analyzed(row):
        report = values.json_object(row["report_json"])
        return report.get("analyzed_at") or ""

    rows = [
        row
        for row in rows
        if not row["fingerprint"]
        or not analyzed(row)
        or utc_timestamp(analyzed(row)).astimezone(TIMEZONE).date() != today
    ]
    stale = sorted(rows, key=lambda row: (analyzed(row), row["created_at"], row["proposal_id"]))
    priority = sorted(
        rows,
        key=lambda row: (
            {"high": 0, "medium": 1}.get(values.json_object(row["report_json"]).get("recommendation"), 2),
            analyzed(row),
            row["proposal_id"],
        ),
    )
    first = iter([row for row in stale if not analyzed(row)])
    stale = iter(stale)
    top = iter(priority)
    selected = {}
    for lane in (first, top, stale) * limit:
        for row in lane:
            if row["proposal_id"] not in selected:
                selected[row["proposal_id"]] = row
                break
        if len(selected) == limit:
            break
    return selected.values()


def _routing_suggestion(setting, endpoint, source, comments):
    prompt = proposal_prompts.routing(source)
    evidence = {"ticket": {key: source[key] for key in ("title", "description")}, "discussion": comments["items"]}
    output = model_gateway.request_json(
        instructions=prompt.text,
        evidence=evidence,
        endpoint=endpoint,
        model=setting["model"],
        timeout=30,
        session_id="robert-proposal-route-" + proposals.digest(source["identity"])[:24],
    )
    if (
        not isinstance(output, dict)
        or set(output) != {"label", "reason"}
        or not isinstance(output["reason"], str)
        or not output["reason"].strip()
        or len(output["reason"]) > 500
    ):
        raise ValueError("invalid proposal routing recommendation")
    rule = next((item for item in source["repository_candidates"] if item["label"] == output["label"]), None)
    if output["label"] is not None and not rule:
        raise ValueError("proposal recommended an unavailable repository")
    return rule, safe_text(output["reason"], 500)


def _daily_assessment(config, setting, endpoint, row, source, repos, clients, commits, now, *, observed):
    name = source.get("repository")
    if source["provider"] == "tapd" and source["project_key"] not in setting["tapd_projects"]:
        return None, None
    if source["provider"] == "github" and name not in setting["github_repositories"]:
        return None, None
    repo = repos.get(name)
    if source["provider"] == "tapd" and source["project_key"] not in clients:
        clients[source["project_key"]] = tapd_client.client_for(config["tapd"], config["data_dir"])
    if not observed:
        source, _ = proposal_sources.refresh(config, repo, source, clients=clients)
        if not source:
            return None, None
    if source.get("routing_conflict"):
        report = {"analyzed_at": now, "feasibility": "needs_clarification", "recommendation": "low"}
        return source, (report, proposals.digest(source))
    name = source.get("repository")
    comments = proposal_sources.discussion(
        source,
        clients,
        config["tapd"],
        github_account=repos.get(name, {}).get("github_account"),
        include_automatic=True,
    )
    source = {**source, "discussion_digest": comments["digest"], "discussion": comments["items"]}
    suggestion, routing_reason = None, ""
    if not name and source.get("repository_candidates"):
        selected_repo = next((key for key in repos if values.repository_id(key) == row["repo_id"]), None)
        if selected_repo:
            suggestion = next(
                (rule for rule in source["repository_candidates"] if rule["repository"] == selected_repo), None
            )
        if not suggestion:
            suggestion, routing_reason = _routing_suggestion(setting, endpoint, source, comments)
        name = suggestion["repository"] if suggestion else None
    report = None
    commit = None
    if name in repos:
        if name not in commits:
            commits[name] = proposal_evidence.pinned_commit(repos[name])
        commit = commits[name]
        excerpts = proposal_evidence.excerpts(repos[name], commit, source["title"] + "\n" + source["description"])
        if excerpts:
            measured = {}
            call_id = values.new_id("proposer-model")
            try:
                report = evaluate(
                    setting,
                    endpoint,
                    {**source, "repository": name},
                    commit,
                    excerpts,
                    comments,
                    on_usage=measured.update,
                )
            finally:
                scope = values.repository_id(name) + "/" + source["identity"]
                with closing(database.connect(config["db_path"])) as conn, conn:
                    model_usage.record(conn, "proposer", call_id, scope, now, measured)
    if report:
        report.setdefault("recommendation", "medium")
        report.setdefault("recommendation_reason", report["summary"])
        report["feasibility"] = "ready" if report["kind"] == "implementation" else "investigate"
    else:
        clarification = "需要确认目标仓库。" if not name else "现有证据不足以确定可验证的处理范围。"
        report = {
            "summary": clarification,
            "feasibility": "needs_clarification",
            "recommendation": "low",
            "recommendation_reason": clarification,
        }
    report.update(
        analyzed_at=now,
        repository=name,
        source_priority=source.get("priority"),
        suggested_label=suggestion["label"] if suggestion else None,
        routing_reason=routing_reason,
    )
    fingerprint = proposals.digest([source, commit])
    return source, (report, fingerprint)


def scan_daily(config, *, now):
    """Discover durable candidates and refresh the daily analysis budget."""
    stats = _daily_stats()
    setting = config["proposer"]
    db_path = config["db_path"]
    web_setup.prepare_repositories(config)
    until = utc_timestamp(now)
    repos = {repo["full_name"]: repo for repo in config["repos"]}
    clients, commits = {}, {}
    recent = {
        source["identity"]: source
        for source in proposal_sources.discover(config, until - timedelta(days=7), until, stats, clients)
    }
    with closing(database.connect(db_path)) as conn, conn:
        _remember_daily(conn, recent.values(), now, stats)
        queue = _daily_queue(conn, setting["schedule"]["analysis_limit"], until.astimezone(TIMEZONE).date())
    try:
        endpoint = model_gateway.gateway_endpoint(setting["gateway_metadata"])
    except _NETWORK_ERRORS:
        return {**stats, "ok": False, "status": "model_unavailable"}
    for row in queue:
        stats["evaluated"] += 1
        source = recent.get(row["source_identity"]) or values.json_object(row["source_json"])
        try:
            current, result = _daily_assessment(
                config,
                setting,
                endpoint,
                row,
                source,
                repos,
                clients,
                commits,
                now,
                observed=row["source_identity"] in recent,
            )
        except commands.ControlDeadlineExceeded:
            raise
        except _NETWORK_ERRORS:
            stats["failed_evaluations"] += 1
            continue
        with closing(database.connect(db_path)) as conn, conn:
            transactions.begin_immediate(conn)
            latest = queries.observation(conn, row["source_identity"])
            if not latest or latest["version"] != row["version"]:
                continue
            if current is None or _source_occupied(conn, current, latest, scheduled=True):
                if latest["status"] != "blocked":
                    store.revise(
                        conn,
                        latest["proposal_id"],
                        latest["version"],
                        status="blocked",
                        version=latest["version"] + 1,
                        updated_at=now,
                        blocked_reason="原单已开始、关闭或移出当前来源范围。",
                    )
                continue
            report, fingerprint = result
            _daily_revision(conn, current, now, previous=latest, report=report, fingerprint=fingerprint)
            stats["updated"] += 1
    if stats["failed_scopes"] or stats["failed_evaluations"]:
        stats["status"] = "partial"
    return stats
