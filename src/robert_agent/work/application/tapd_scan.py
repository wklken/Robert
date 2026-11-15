"""TAPD comment discovery and tracked-ticket refresh inside the scan lane."""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import subprocess

from robert_agent.adapters.tapd import client as tapd_client
from robert_agent.foundation import tapd_identity, values
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import database as db_database
from robert_agent.persistence import records as db_records
from robert_agent.persistence import transactions as db_transactions
from robert_agent.persistence.queries import github_actions as db_read_github_actions
from robert_agent.persistence.queries import github_events as db_read_github_events
from robert_agent.persistence.queries import github_sources as db_read_github_sources
from robert_agent.persistence.queries import notifications as db_read_notifications
from robert_agent.persistence.queries import tapd_scan_state as db_read_tapd_scan_state
from robert_agent.persistence.stores import github_events as db_write_github_events
from robert_agent.persistence.stores import github_sources as db_write_github_sources
from robert_agent.persistence.stores import notifications as db_write_notifications
from robert_agent.persistence.stores import tapd_scan_state as db_write_tapd_scan_state
from robert_agent.work import queries as work_queries
from robert_agent.work import repositories as work_repositories
from robert_agent.work import tapd_policy

OVERLAP = timedelta(minutes=10)
MAX_TEXT_CHARS = 20000
MAX_COMMENT_CHARS = 4000
MAX_SNAPSHOT_COMMENTS = 100
PROTOCOL_ERRORS = (tapd_client.TapdError, subprocess.TimeoutExpired, OSError, ValueError)
_TIME = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class Ticket:
    item: tapd_client.TapdItem
    comments: tuple
    terminal_states: dict

    @property
    def ended(self):
        return tapd_policy.is_ended(self.item.status, self.terminal_states)


def _reason(exc):
    return exc.code if isinstance(exc, tapd_client.TapdError) else type(exc).__name__


def _position(created, comment_id):
    return created, int(comment_id)


def _since(project, watermark):
    start = utc_timestamp(project["accept_after"]).astimezone(tapd_policy.TAPD_TIMEZONE)
    if watermark is not None:
        overlap = tapd_policy.parse_time(watermark[0]) - OVERLAP
        start = max(start, overlap)
    return start.strftime(_TIME)


def _first_observation(comment, watermark):
    """An edit below the watermark cannot turn an observed comment into a new request."""
    return (
        watermark is None
        or _position(comment.created, comment.comment_id) > _position(*watermark)
        or comment.modified in (None, comment.created)
    )


def _comment_fields(comment):
    return {
        "comment_id": comment.comment_id,
        "author": comment.author,
        "created": comment.created,
        "body": comment.body,
    }


def _decision(comment, project, settings, automatic_ids, watermark, *, known):
    decision = tapd_policy.authorize_comment(
        _comment_fields(comment),
        project,
        mention_name=settings["mention_name"],
        automatic_ids=automatic_ids,
        known=known,
    )
    if decision["drives_execution"] and not _first_observation(comment, watermark):
        return {"authorization_status": "ignored_context", "creates_task": False, "drives_execution": False}
    return decision


def _clip(text, limit):
    return text[:limit], len(text) > limit


def _read_ticket(client, workspace_id, kind, external_id, terminal_cache):
    item = client.get_item(workspace_id, kind, external_id)
    comments = client.ticket_comments(workspace_id, kind, external_id)
    type_key = (kind, item.workitem_type_id if kind == "story" else None)
    if type_key not in terminal_cache:
        terminal_cache[type_key] = client.terminal_states(workspace_id, kind, type_key[1])
    return Ticket(item, comments, terminal_cache[type_key])


def _snapshot(ticket, settings, automatic_ids, repository, label, now):
    mention_name = settings["mention_name"]
    description, description_clipped = _clip(
        tapd_policy.parse_comment(ticket.item.description, mention_name).text, MAX_TEXT_CHARS
    )
    comments = []
    for comment in ticket.comments[-MAX_SNAPSHOT_COMMENTS:]:
        parsed = tapd_policy.parse_comment(comment.body, mention_name)
        text, clipped = _clip(parsed.text, MAX_COMMENT_CHARS)
        comments.append(
            {
                "comment_id": comment.comment_id,
                "author": comment.author,
                "created": comment.created,
                "text": text,
                "truncated": clipped,
                "automatic": parsed.automatic or comment.comment_id in automatic_ids,
            }
        )
    return {
        "kind": ticket.item.kind,
        "external_id": ticket.item.external_id,
        "title": ticket.item.title,
        "status": ticket.item.status,
        "labels": tapd_policy.split_labels(ticket.item.labels),
        "ended": ticket.ended,
        "repository": repository,
        "repository_label": label,
        "description": description,
        "description_truncated": description_clipped,
        "comments": comments,
        "omitted_comments": max(0, len(ticket.comments) - MAX_SNAPSHOT_COMMENTS),
        "observed_at": now,
    }


def _event(project, settings, ticket, comment, decision, *, repository, label, source_key, automatic_ids):
    host, workspace_id = project["host"], project["workspace_id"]
    kind, external_id = ticket.item.kind, ticket.item.external_id
    parsed = tapd_policy.parse_comment(comment.body, settings["mention_name"])
    body, _clipped = _clip(parsed.text, MAX_TEXT_CHARS)
    return {
        "provider": "tapd",
        "repo": repository,
        "repository_label": label,
        "source_type": tapd_identity.SOURCE_TYPES[kind],
        "number": None,
        "project_key": tapd_identity.project_key(host, workspace_id),
        "external_id": external_id,
        "source_key": source_key,
        "workstream_id": source_key,
        "event_fingerprint": tapd_identity.comment_fingerprint(host, workspace_id, comment.comment_id),
        "event_type": "comment",
        "event_at": tapd_policy.parse_time(comment.created).astimezone(timezone.utc).isoformat(),
        "actor_kind": "tapd_user",
        "actor_login": comment.author,
        "body": body,
        "url": tapd_identity.ticket_url(host, workspace_id, kind, external_id),
        "title": ticket.item.title,
        "state": "closed" if ticket.ended else "open",
        "comment_id": comment.comment_id,
        "comment_created": comment.created,
        "automatic": parsed.automatic or comment.comment_id in automatic_ids,
        "mentions_dd": parsed.mentions,
        **decision,
    }


def _record_source(conn, project, settings, ticket, repo, label, automatic_ids, watermark, now):
    """Write one ticket's source row and its trusted user's comments in the caller transaction."""
    host, workspace_id = project["host"], project["workspace_id"]
    kind, external_id = ticket.item.kind, ticket.item.external_id
    repo_id = work_repositories.upsert_repo(conn, repo)
    source_key = tapd_identity.source_key(host, workspace_id, kind, external_id, repo["full_name"])
    source_id = values.github_source_id(source_key)
    db_write_github_sources.upsert_tapd_source(
        conn,
        source_id,
        repo_id,
        source_key,
        tapd_identity.SOURCE_TYPES[kind],
        tapd_identity.project_key(host, workspace_id),
        external_id,
        tapd_identity.ticket_url(host, workspace_id, kind, external_id),
        ticket.item.title,
        "closed" if ticket.ended else "open",
        now,
        json.dumps(
            {"tapd": _snapshot(ticket, settings, automatic_ids, repo["full_name"], label, now)},
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
    known = source_key in work_queries.load_known_workstreams(conn, repo_id)
    started = utc_timestamp(project["accept_after"])
    trusted = [
        comment
        for comment in ticket.comments
        if comment.author == project["trusted_user"]
        and (observed := tapd_policy.parse_time(comment.created)) is not None
        and observed >= started
    ]  # Other people's and earlier comments are context in the snapshot only.
    fingerprints = [tapd_identity.comment_fingerprint(host, workspace_id, item.comment_id) for item in trusted]
    noticed = {row[0] for row in db_read_notifications.find_existing_ids(conn, map(label_notice_id, fingerprints))}
    written = 0
    for comment, fingerprint in zip(trusted, fingerprints):
        decision = _decision(comment, project, settings, automatic_ids, watermark, known=known)
        if label_notice_id(fingerprint) in noticed:
            decision = {"authorization_status": "ignored_context", "creates_task": False, "drives_execution": False}
        event = _event(
            project,
            settings,
            ticket,
            comment,
            decision,
            repository=repo["full_name"],
            label=label,
            source_key=source_key,
            automatic_ids=automatic_ids,
        )
        written += db_write_github_events.insert_tapd_event_if_absent(
            conn,
            values.github_event_id(event["event_fingerprint"]),
            repo_id,
            source_id,
            event["event_fingerprint"],
            event["event_type"],
            comment.author,
            decision["authorization_status"],
            event["event_at"],
            json.dumps(event, ensure_ascii=False, sort_keys=True),
        )
    return written


def label_notice_id(fingerprint):
    return tapd_policy.notice_id(tapd_policy.LABEL_NOTICE, fingerprint)


def _record_label_notices(conn, project, ticket, triggers, outcome, now):
    host, workspace_id = project["host"], project["workspace_id"]
    for comment in triggers:
        fingerprint = tapd_identity.comment_fingerprint(host, workspace_id, comment.comment_id)
        metadata = {
            "project_key": tapd_identity.project_key(host, workspace_id),
            "kind": ticket.item.kind,
            "external_id": ticket.item.external_id,
            "url": tapd_identity.ticket_url(host, workspace_id, ticket.item.kind, ticket.item.external_id),
            "dedupe_key": fingerprint,
            "reason": outcome,
            "labels": [rule["label"] for rule in project["label_rules"]],
        }
        db_write_notifications.insert_tapd_notification(
            conn,
            label_notice_id(fingerprint),
            tapd_policy.LABEL_NOTICE,
            now,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        )


def _write(db_path, operation, *args):
    with closing(db_database.connect(db_path, timeout=5)) as conn, conn:
        db_transactions.begin(conn, immediate=True)
        return operation(conn, *args)


def _observed(db_path, fingerprints):
    """Comments already stored as events or already answered with a label notice."""
    with closing(db_database.connect(db_path, timeout=5)) as conn:
        events = db_read_github_events.find_existing_fingerprints(conn, fingerprints)
        notices = db_read_notifications.find_existing_ids(conn, [label_notice_id(item) for item in fingerprints])
    noticed = {row[0] for row in notices}
    return {row[0] for row in events} | {item for item in fingerprints if label_notice_id(item) in noticed}


def scan_project(db_path, settings, project, repos, client, *, now=None):
    """Discover new trusted requests, then refresh tickets that still have unfinished work."""
    now = now or datetime.now(timezone.utc).isoformat()
    host, workspace_id = project["host"], project["workspace_id"]
    key = tapd_identity.project_key(host, workspace_id)
    summary = {"project_key": key, "events": 0, "label_notices": 0, "refreshed": 0, "failures": []}
    with closing(db_database.connect(db_path, timeout=5)) as conn:
        stored = db_records.first(db_read_tapd_scan_state.find_watermark(conn, key))
        recorded = db_read_github_actions.find_tapd_comment_ids(conn, f"https://{host}/{workspace_id}/")
    watermark = (stored[0], stored[1]) if stored else None
    automatic_ids = {row[0] for row in recorded}
    try:
        comments = client.author_comments(workspace_id, project["trusted_user"], since=_since(project, watermark))
    except ControlDeadlineExceeded:
        raise
    except PROTOCOL_ERRORS as exc:
        summary["failures"].append({"stage": "discovery", "reason": _reason(exc)})
        return summary
    fingerprints = [tapd_identity.comment_fingerprint(host, workspace_id, item.comment_id) for item in comments]
    observed = _observed(db_path, fingerprints)
    triggers = [
        comment
        for comment, fingerprint in zip(comments, fingerprints)
        if fingerprint not in observed
        and _decision(comment, project, settings, automatic_ids, watermark, known=False)["drives_execution"]
    ]
    complete, refreshed, terminal_cache = True, set(), {}
    for kind, external_id in dict.fromkeys((item.kind, item.entry_id) for item in triggers):
        try:
            ticket = _read_ticket(client, workspace_id, kind, external_id, terminal_cache)
        except ControlDeadlineExceeded:
            raise
        except tapd_client.TapdError as exc:
            if exc.code != "not_found":
                complete = False
            summary["failures"].append({"stage": "ticket", "external_id": external_id, "reason": exc.code})
            continue
        except PROTOCOL_ERRORS as exc:
            complete = False
            summary["failures"].append({"stage": "ticket", "external_id": external_id, "reason": _reason(exc)})
            continue
        ticket_triggers = [item for item in triggers if (item.kind, item.entry_id) == (kind, external_id)]
        outcome, repository, label = tapd_policy.select_repository(ticket.item.labels, project["label_rules"])
        if outcome != "selected":
            if not ticket.ended:
                _write(db_path, _record_label_notices, project, ticket, ticket_triggers, outcome, now)
                summary["label_notices"] += len(ticket_triggers)
            continue
        summary["events"] += _write(
            db_path, _record_source, project, settings, ticket, repos[repository], label, automatic_ids, watermark, now
        )
        refreshed.add((kind, external_id, repository))
    with closing(db_database.connect(db_path, timeout=5)) as conn:
        tracked = db_read_github_sources.find_tapd_tracked_sources(conn, key)
    for _source_id, _repo_id, _source_key, source_type, external_id, metadata_json in tracked:
        snapshot = values.json_object(metadata_json).get("tapd") or {}
        kind, repository = tapd_identity.KINDS[source_type], snapshot.get("repository")
        if repository not in repos or (kind, external_id, repository) in refreshed:
            continue
        try:
            ticket = _read_ticket(client, workspace_id, kind, external_id, terminal_cache)
        except ControlDeadlineExceeded:
            raise
        except PROTOCOL_ERRORS as exc:
            summary["failures"].append({"stage": "tracked", "external_id": external_id, "reason": _reason(exc)})
            continue
        label = snapshot.get("repository_label")
        summary["events"] += _write(
            db_path, _record_source, project, settings, ticket, repos[repository], label, automatic_ids, watermark, now
        )
        summary["refreshed"] += 1
    if complete and comments:
        newest = max(comments, key=lambda item: _position(item.created, item.comment_id))
        if watermark is None or _position(newest.created, newest.comment_id) > _position(*watermark):
            _write(db_path, db_write_tapd_scan_state.save_watermark, key, newest.created, newest.comment_id, now)
    return summary


def scan(config, *, runner=None, environment=None, now=None):
    """Run every configured TAPD project; failures stay diagnostics and never stop GitHub scanning."""
    settings = config.get("tapd") or {}
    if not settings.get("enabled"):
        return {"enabled": False, "projects": []}
    try:
        client = tapd_client.client_for(settings, config["data_dir"], runner=runner, environment=environment)
    except tapd_client.TapdError as exc:
        return {"enabled": True, "projects": [], "failures": [{"stage": "client", "reason": exc.code}]}
    repos = {repo["full_name"]: repo for repo in config["repos"]}
    return {
        "enabled": True,
        "projects": [
            scan_project(config["db_path"], settings, project, repos, client, now=now)
            for project in settings["projects"]
        ],
    }
