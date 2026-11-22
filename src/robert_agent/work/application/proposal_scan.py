"""Scan for bounded proposals and preserve distinct evidence revisions."""

from contextlib import closing
from datetime import timedelta
import subprocess
from zoneinfo import ZoneInfo

from robert_agent.adapters.git import proposal_evidence
from robert_agent.adapters.github import issues
from robert_agent.adapters.services import supervision_model as model_gateway
from robert_agent.adapters.tapd import client as tapd_client
from robert_agent.context import proposal_prompts
from robert_agent.foundation import commands, tapd_identity, values
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import proposals as queries
from robert_agent.persistence.stores import proposals as store
from robert_agent.work import proposal_feedback, proposals, tapd_policy
from robert_agent.work.application import proposal_sources, web_setup


TIMEZONE = ZoneInfo("Asia/Shanghai")
BATCH_LIMIT = 6
MUTABLE_STATES = frozenset({"evaluated", "pending", "held", "blocked"})
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


def _source_occupied(conn, source, previous):
    if previous and previous["status"] == "blocked":
        binding = queries.work_binding(conn, source["source_key"])
        return bool(binding and binding["active_task_id"])
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


def _github_candidates(setting, repos, since, until, stats):
    for name in sorted(setting["github_repositories"]):
        try:
            items = issues.list_recent(name, since=since, until=until)
        except commands.ControlDeadlineExceeded:
            raise
        except _NETWORK_ERRORS:
            stats["failed_scopes"].append("github:" + name)
            continue
        for issue in items:
            source = proposals.github_source(repos[name], issue, setting["github_assignees"])
            if source:
                yield source


def _tapd_candidates(setting, config, since, until, stats, clients):
    if not setting["tapd_projects"]:
        return
    for project in config["tapd"]["projects"]:
        workspace = project["workspace_id"]
        key = tapd_identity.project_key(project["host"], workspace)
        if key not in setting["tapd_projects"]:
            continue
        try:
            client = tapd_client.client_for(config["tapd"], config["data_dir"])
            clients[key] = client
        except _NETWORK_ERRORS:
            stats["failed_scopes"].append("tapd:" + key)
            continue
        terminal_cache = {}
        for kind in ("story", "bug"):
            try:
                items = client.recent_items(workspace, kind, since=since, until=until)
                for item in items:
                    _, route, _ = tapd_policy.select_repository(item.labels, project["label_rules"])
                    if (
                        not route
                        or item.owners is None
                        or any(owner not in setting["tapd_assignees"] for owner in item.owners)
                    ):
                        continue
                    terminal_key = (kind, item.workitem_type_id)
                    if terminal_key not in terminal_cache:
                        terminal_cache[terminal_key] = client.terminal_states(
                            workspace, kind, item.workitem_type_id if kind == "story" else None
                        )
                    source = proposals.tapd_source(
                        project, item, terminal_cache[terminal_key], setting["tapd_assignees"]
                    )
                    if source:
                        yield source
            except commands.ControlDeadlineExceeded:
                raise
            except _NETWORK_ERRORS:
                stats["failed_scopes"].append(f"tapd:{key}:{kind}")


def evaluate(setting, endpoint, source, commit, excerpts, comments):
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
        *_github_candidates(setting, repos, since, until, stats),
        *_tapd_candidates(setting, config, since, until, stats, clients),
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
            report = evaluate(setting, endpoint, source, commit, excerpts, comments) if excerpts else None
        except commands.ControlDeadlineExceeded:
            raise
        except _NETWORK_ERRORS:
            stats["failed_evaluations"] += 1
            continue
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
