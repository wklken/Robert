"""Observe native TAPD facts and freeze authorized work under the source fence."""

import json
from dataclasses import asdict, dataclass, replace

from robert_agent.execution import ownership
from robert_agent.foundation.contracts.execution import ModelRunStatus
from robert_agent.foundation.contracts.work import SourceAuthorizationType as Auth
from robert_agent.foundation.contracts.work import SourceIngressPhase as Phase
from robert_agent.foundation.contracts.work import SourceRoundStatus
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import records, transactions
from robert_agent.persistence.queries import ingress_requests as read_requests
from robert_agent.persistence.queries import (
    source_confirmation,
    source_delivery_pauses,
    source_delivery_recovery,
    source_members,
    source_results,
    source_retirement,
    source_tasks,
    sources,
)
from robert_agent.persistence.queries import (
    source_github_ingress as github_read,
)
from robert_agent.persistence.queries import source_sessions as read_sessions
from robert_agent.persistence.queries import (
    tapd_intake as read,
)
from robert_agent.persistence.rounds import source_model
from robert_agent.persistence.stores import (
    ingress_requests,
    source_sessions,
)
from robert_agent.persistence.stores import (
    source_confirmation as confirmation_store,
)
from robert_agent.persistence.stores import (
    tapd_intake as write,
)
from robert_agent.persistence.stores.sources import upsert_tapd_source
from robert_agent.work import source_confirmation as confirmation_policy
from robert_agent.work import tapd_authority as authority
from robert_agent.work.application import source_confirmations, source_github_observation, source_request_authority
from robert_agent.work.tapd_policy import parse_comment

_COMMENT = "tapd_comment"
_QUEUE = "tapd_label_queue"
_REVOKED = {
    "terminal",
    "comment_deleted",
    "mention_removed",
    "creator_changed",
    "automated_comment",
    "confirmation_command",
}
_CANCELLING = _REVOKED | {
    "confirmation_required",
    "repository_change_requires_confirmation",
    "repository_scope_removed",
}


@dataclass(frozen=True, slots=True)
class IntakeResult:
    source_id: str
    revision: str | None
    reason: str
    request_ids: tuple[str, ...] = ()
    cancel_round_id: str | None = None
    cancel_task_ids: tuple[str, ...] = ()
    cancel_model_run_ids: tuple[str, ...] = ()


def _project(config, ticket):
    if config.get("host") != ticket.host:
        raise ValueError("TAPD observation is outside configured host")
    found = [project for project in config["projects"] if project["workspace_id"] == ticket.workspace_id]
    if len(found) != 1:
        raise ValueError("TAPD observation is outside configured project")
    return found[0]


def _load(conn, ticket):
    row = sources.find_by_identity(
        conn,
        provider="tapd",
        host=ticket.host,
        project_key=ticket.workspace_id,
        source_type=ticket.kind,
        external_id=ticket.external_id,
    )
    return {} if row is None else json.loads(row["metadata_json"])


def _configuration(config, project):
    return authority.digest(
        {
            "host": config["host"],
            "api_timezone": config["api_timezone"],
            "bot_name": config["bot_name"],
            "reply_name": config["reply_name"],
            "project": project,
        }
    )


def _activation(conn, ticket, project, now):
    if not project["mentions"]["enabled"]:
        return None, "mentions_disabled"
    cutoff = utc_timestamp(project["mentions"].get("activated_after"))
    if cutoff is None:
        return None, "activation_unknown"
    saved = write.remember_activation(conn, ticket.host, ticket.workspace_id, cutoff.isoformat(), now)
    return (cutoff, "enabled") if utc_timestamp(saved) == cutoff else (None, "activation_drift")


def _queue_binding(previous, label, state):
    if not previous:
        return None
    if state == "terminal":
        return {**previous, "state": "terminal", "reason": state}
    if previous["state"] != "active":
        return previous
    if label.reason == "allowed" and label.rule == previous["rule"]:
        return previous
    if label.reason in ("ticket_state_unknown", "label_facts_unknown"):
        return previous
    reason = "repository_change_requires_confirmation" if label.reason == "allowed" else label.reason
    return {**previous, "state": "paused", "reason": reason}


def _save_comment(conn, source_id, event_id, data):
    write.save_event(
        conn,
        source_id=source_id,
        event_id=event_id,
        event_type=_COMMENT,
        happened_at=data["first"]["observed_at"],
        payload=data,
    )


def _observe_comments(conn, config, project, observation, publication, cutoff, binding, initial_rule, now):
    ticket = observation.facts.identity
    source_id = ticket.source_id
    current = {}
    seen = utc_timestamp(observation.observed_at)
    written = utc_timestamp(now)
    for comment in sorted(observation.comments, key=lambda row: row.version.event_id):
        event_id = comment.version.event_id
        own = (comment.version.entry_type, comment.version.external_id) in publication.own_comments
        payload = comment.payload(bot_name=config["bot_name"], reply_name=config["reply_name"], bot_owned=own)
        payload["observed_at"] = observation.observed_at
        created = authority.remote_timestamp(payload["created"], config["api_timezone"])
        payload["created_at_utc"] = created.isoformat() if created is not None else None
        old = read.event(conn, event_id)
        previous = json.loads(old["payload_json"]) if old is not None else None
        if previous is None:
            tag_rule = binding["rule"] if binding and binding["state"] == "active" else None
            boundary = utc_timestamp(binding["initial_observed_at"]) if tag_rule else None
            trusted = (
                created is not None
                and created <= written
                and authority.comment_authority(payload, project) == "allowed"
            )
            if trusted and initial_rule is not None:
                tag_rule = initial_rule
                disposition = "after_boundary" if created > seen else "absorbed"
            elif trusted and tag_rule is not None and created > boundary:
                disposition = "eligible"
            elif trusted and tag_rule is not None:
                disposition = "absorbed"
            elif trusted and cutoff is not None and created >= cutoff:
                disposition = "eligible"
            else:
                disposition = "context"
            data = {
                "first": payload,
                "latest": payload,
                "disposition": disposition,
                "entry_rule": tag_rule,
                "first_saved_at": now,
            }
        else:
            prior = previous["latest"]
            # Durable bot ownership and tombstones survive render/config drift.
            payload["bot_owned"] |= bool(prior.get("bot_owned"))
            if prior.get("deleted"):
                payload["deleted"] = prior["deleted"]
            data = {**previous, "latest": payload}
            if (
                previous["disposition"] in ("eligible", "after_boundary", "queued")
                and authority.comment_authority(payload, project) in _REVOKED
            ):
                data["disposition"] = "revoked"
        _save_comment(conn, source_id, event_id, data)
        current[event_id] = data
    if observation.coverage == "complete":
        for absence in observation.absences:
            event_id = absence.event_id
            old = read.event(conn, event_id)
            if old is None:
                payload = {
                    "observed_at": observation.observed_at,
                    "entry_type": absence.entry_type,
                    "external_id": absence.external_id,
                    "deleted": absence.evidence_id,
                }
                data = {"first": payload, "latest": payload, "disposition": "context"}
            else:
                data = json.loads(old["payload_json"])
                data["latest"] = {**data["latest"], "deleted": absence.evidence_id, "observed_at": now}
            if data["disposition"] in ("eligible", "after_boundary", "queued"):
                data["disposition"] = "revoked"
            _save_comment(conn, source_id, event_id, data)
            current[event_id] = data
    return current


def _version_payload(comment):
    """Retain provider content and principals while excluding per-observation proof IDs."""
    proof = {"observed_at", "creator_evidence", "own_body_evidence", "modified", "body_html", "body_sha256"}
    semantic = {key: True if key == "deleted" else value for key, value in comment.items() if key not in proof}
    if "body_html" in comment:
        parsed = parse_comment(
            comment["body_html"],
            reply_id=comment.get("reply_id"),
            **{key: comment[key] for key in ("bot_name", "reply_name") if key in comment},
        )
        semantic["discussion_segments"] = [(part.kind, part.text.strip("\n")) for part in parsed.segments]
        # The structural parser can omit unsupported content. Keep the bytes
        # as an ambiguity boundary when it could not verify the structure.
        if not parsed.structure_verified:
            semantic["unverified_body"] = comment["body_html"]
    return semantic


def _request_version(snapshot):
    if snapshot.get("provider") == "github":
        return source_members.github_content_version(snapshot["event_id"], snapshot["body"], snapshot["actor_login"])
    if "comment" in snapshot:
        snapshot = {**snapshot, "comment": _version_payload(snapshot["comment"])}
    return authority.digest(snapshot)


def _semantic_input(ticket, discussion, requests):
    return authority.digest(
        {
            "ticket": ticket,
            "discussion": [
                _version_payload(
                    {key: value for key, value in row.items() if key not in {"request_scope", "request_id"}}
                )
                for row in discussion
            ],
            "requests": sorted((row["request_id"], _request_version(row["input"])) for row in requests),
        }
    )


def _registration(conn, ticket, auth_type, authorization, snapshot, event_id, now):
    request_id = "tapd-request:" + authority.digest((ticket.source_id, auth_type.value, event_id))
    row = read_requests.find_by_id(conn, request_id)
    if row is not None:
        return request_id
    ingress_requests.enqueue(
        conn,
        request_id=request_id,
        source_id=ticket.source_id,
        provider="tapd",
        auth_type=auth_type.value,
        authorization=authorization,
        content_snapshot=snapshot,
        content_version=_request_version(snapshot),
        event_ids=(event_id,),
        now=now,
    )
    names = authorization["repos"]
    if authorization.get("rule") is not None and len(names) == 1:
        ids = read.repository_ids(conn, names)
        if names[0] in ids:
            write.amend_request(conn, source_id=ticket.source_id, request_id=request_id, repo_id=ids[names[0]], now=now)
    return request_id


def _initial_request(conn, ticket, facts, label, observation, now):
    event_id = "tapd-initial:" + authority.digest(ticket.key)
    snapshot = {"event_id": event_id, "ticket": asdict(facts)}
    write.save_event(
        conn, source_id=ticket.source_id, event_id=event_id, event_type=_QUEUE, happened_at=now, payload=snapshot
    )
    authorization = {
        "version": 1,
        "capability": label.capability,
        "repos": list(label.repos),
        "rule": label.rule,
        "event_id": event_id,
    }
    request_id = _registration(conn, ticket, Auth.LABEL_QUEUE, authorization, snapshot, event_id, now)
    binding = {
        "state": "active",
        "rule": label.rule,
        "initial_observed_at": observation.observed_at,
        "request_id": request_id,
    }
    return request_id, binding


def _queue_comments(conn, ticket, current, project, facts, label, binding, observing_initial, held, activation, now):
    queued = []
    for event_id, data in current.items():
        if data["disposition"] not in ("eligible", "after_boundary"):
            continue
        payload = data["latest"]
        active_tag = binding and binding["state"] == "active"
        boundary = utc_timestamp(binding["initial_observed_at"]) if active_tag else None
        created = utc_timestamp(data["first"].get("created_at_utc"))
        if data["disposition"] == "after_boundary" and (
            observing_initial or boundary is None or created is None or created <= boundary
        ):
            continue
        if observing_initial and boundary is not None and created is not None and created > boundary:
            continue
        if observing_initial:
            disposition = "absorbed"
        elif (
            held
            or authority.ticket_state(facts) != "open"
            or label.reason in ("repository_conflict", "label_facts_unknown")
            or authority.comment_authority(payload, project) != "allowed"
            or data["first"]["creator"] != project["trusted_author"]
        ):
            continue
        else:
            tag_rule = binding["rule"] if active_tag and label.reason == "allowed" else None
            rule = data.get("entry_rule")
            if data["disposition"] == "after_boundary" and rule is None:
                # Prerelease observations recorded this explicit disposition
                # before the first tag binding was stored.
                rule = tag_rule
            if (rule is not None and rule != tag_rule) or (rule is None and activation != "enabled"):
                continue
            repos = (rule["repo"],) if rule else project["repositories"]
            authorization = {
                "version": 1,
                "capability": "development" if rule else "readonly",
                "repos": list(repos),
                "rule": rule,
                "event_id": event_id,
            }
            snapshot = {"event_id": event_id, "comment": payload}
            request_id = _registration(conn, ticket, Auth.TAPD_MENTION, authorization, snapshot, event_id, now)
            queued.append(request_id)
            disposition = "queued"
        data["disposition"] = disposition
        _save_comment(conn, ticket.source_id, event_id, data)
    return queued


def _request_decision(row, *, facts, project, label, binding, current, activation, delivery=False):
    saved = json.loads(row["authorization_json"])
    data = current.get(saved.get("event_id"))
    if delivery and data and data.get("disposition") == "revoked":
        return authority.AuthorityDecision("request_revoked")
    if data and str(data.get("disposition", "")).startswith("confirmation_"):
        return authority.AuthorityDecision("confirmation_command")
    payload = data["latest"] if data else None
    decision = authority.request_authority(
        row["auth_type"], saved, facts=facts, project=project, label=label, binding=binding, comment=payload
    )
    if (
        decision.reason == "confirmation_required"
        and binding
        and binding.get("reason") == "resume_confirmation_unavailable"
    ):
        return authority.AuthorityDecision("resume_confirmation_unavailable")
    if (
        decision.reason == "allowed"
        and row["auth_type"] == Auth.TAPD_MENTION
        and saved.get("rule") is None
        and activation != "enabled"
    ):
        return authority.AuthorityDecision("mention_entry_unavailable")
    return decision


def _refresh_requests(conn, ticket, facts, project, label, binding, current, held, activation, now):
    active = read_sessions.active_round(conn, ticket.source_id)
    frozen = {row["request_id"] for row in read_sessions.round_requests(conn, active["round_id"])} if active else set()
    for row in read.pending_requests(conn, ticket.source_id):
        request_id = row["request_id"]
        if row["phase"] == Phase.REVOKED:
            continue
        decision = _request_decision(
            row, facts=facts, project=project, label=label, binding=binding, current=current, activation=activation
        )
        phase = row["phase"]
        reason = decision.reason
        if reason in _REVOKED:
            phase = Phase.REVOKED.value
        elif reason in _CANCELLING:
            phase = Phase.PAUSED.value
        elif reason == "allowed" and phase != Phase.PAUSED:
            phase = Phase.HELD.value if held else Phase.PENDING.value
        elif phase == Phase.PENDING:
            phase = Phase.HELD.value
        changes = {"phase": phase} if phase != row["phase"] else {}
        event_id = json.loads(row["authorization_json"]).get("event_id")
        snapshot = None
        if reason == "allowed":
            if row["auth_type"] == Auth.TAPD_MENTION and event_id in current:
                snapshot = {"event_id": event_id, "comment": current[event_id]["latest"]}
            elif row["auth_type"] == Auth.LABEL_QUEUE:
                snapshot = {"event_id": event_id, "ticket": asdict(facts)}
        if snapshot is not None:
            version = _request_version(snapshot)
            if row["content_version"] != version:
                changes.update(snapshot=snapshot, version=version)
                if row["auth_type"] == Auth.LABEL_QUEUE:
                    write.save_event(
                        conn,
                        source_id=ticket.source_id,
                        event_id=event_id,
                        event_type=_QUEUE,
                        happened_at=now,
                        payload=snapshot,
                    )
        if changes:
            write.amend_request(conn, source_id=ticket.source_id, request_id=request_id, now=now, **changes)
        if request_id in frozen and reason in _CANCELLING:
            write.request_cancellation(
                conn, source_id=ticket.source_id, round_id=active["round_id"], reason=reason, now=now
            )
    if active and source_request_authority.pause_active_parent_requests(conn, active, label, binding, now=now):
        write.request_cancellation(
            conn,
            source_id=ticket.source_id,
            round_id=active["round_id"],
            reason="github_development_binding_paused",
            now=now,
        )
    if active:
        # A consumed root can still be awaiting receipts. Retain its ledger
        # and record an explicit newly observed revocation on that same round.
        round_id = active["round_id"]
        for row in read.consumed_root_requests(conn, ticket.source_id, round_id):
            if row["request_id"] not in frozen:
                continue
            decision = _request_decision(
                row,
                facts=facts,
                project=project,
                label=label,
                binding=binding,
                current=current,
                activation=activation,
                delivery=True,
            )
            if decision.reason in _CANCELLING or decision.reason == "request_revoked":
                write.request_cancellation(
                    conn, source_id=ticket.source_id, round_id=round_id, reason=decision.reason, now=now
                )
    if active and authority.ticket_state(facts) == "terminal":
        write.request_cancellation(
            conn, source_id=ticket.source_id, round_id=active["round_id"], reason="terminal", now=now
        )
    if active and read.round_cancellation(conn, active["round_id"]):
        round_id = active["round_id"]
        return (
            round_id,
            tuple(row["task_id"] for row in read_sessions.round_tasks(conn, round_id)),
            tuple(
                row["run_id"]
                for row in read_sessions.model_runs(conn, round_id)
                if row["status"] in ("waiting", "starting", "running")
            ),
        )
    return None, (), ()


def _fixed_notice_episode(previous, observation, label):
    old = previous.get("fixed_notice_episode")
    if (
        observation.coverage != "complete"
        or observation.facts.body is None
        or label.reason in {"ticket_state_unknown", "label_facts_unknown"}
    ):
        return old
    repositories = list(label.repos) if label.reason == "repository_conflict" else []
    if old is None:
        # Preserve a legacy notice for the same unresolved conflict on upgrade.
        previous_reason = previous.get("label_reason")
        prior_scope_known = (
            previous.get("coverage") == "complete"
            and (previous.get("ticket") or {}).get("body") is not None
            and previous_reason not in (None, "ticket_state_unknown", "label_facts_unknown")
        )
        previous_repositories = previous.get("label_repos", []) if previous_reason == "repository_conflict" else []
        generation = int(bool(repositories and prior_scope_known and repositories != previous_repositories))
    else:
        generation = old["generation"] + int(bool(repositories and repositories != old["repositories"]))
    return {"generation": generation, "repositories": repositories}


def _observation_revision(metadata, current):
    return authority.digest(
        {
            **{key: value for key, value in metadata.items() if key != "revision"},
            "comments": [data["latest"] for _, data in sorted(current.items())],
        }
    )


def _without_current_resume(binding):
    return {**binding, "state": "paused", "reason": "resume_confirmation_unavailable"}


def _persist_observation(conn, config, project, observation, publication, *, now, previous, observation_hash):
    facts = observation.facts
    ticket = facts.identity
    source_id = ticket.source_id
    cutoff, activation = _activation(conn, ticket, project, now)
    label = authority.label_authority(facts, project)
    state = authority.ticket_state(facts)
    binding = _queue_binding(previous.get("binding"), label, state)
    source = asdict(facts)
    source.pop("identity")
    _save_source(conn, ticket, facts, observation.observed_at, previous)
    held = observation.coverage != "complete" or facts.body is None or bool(publication.unknown_intents)
    initializing = not binding and label.reason == "allowed" and not held
    current = _observe_comments(
        conn, config, project, observation, publication, cutoff, binding, label.rule if initializing else None, now
    )
    choices = source_confirmations.intercept(conn, config, project, observation, publication, binding, current, now=now)
    for event_id, disposition in choices.items():
        current[event_id]["disposition"] = disposition
        _save_comment(conn, source_id, event_id, current[event_id])
    prior_binding = previous.get("binding")
    grant_binding = binding
    if source_confirmations.resume_required(conn, source_id, prior_binding):
        status, answer = source_confirmations.applied_resume_status(
            conn, config, project, facts, prior_binding, current, complete=not held
        )
        if status == "revoked":
            if answer is not None:
                confirmation_store.revoke(conn, answer, now)
            intact_rule_loss = (
                binding.get("reason") == "no_label_match"
                and not held
                and source_confirmations.applied_answer_intact(conn, ticket, binding, project, current)
            )
            if binding["state"] != "terminal" and not intact_rule_loss:
                binding = {**binding, "state": "paused", "reason": "resume_confirmation_revoked"}
            grant_binding = binding
        elif status != "current" and binding["state"] == "active":
            grant_binding = _without_current_resume(binding)
    created = []
    if initializing:
        request_id, binding = _initial_request(conn, ticket, facts, label, observation, now)
        grant_binding = binding
        created.append(request_id)
    created.extend(
        _queue_comments(
            conn, ticket, current, project, facts, label, grant_binding, initializing, held, activation, now
        )
    )
    cancellation = _refresh_requests(conn, ticket, facts, project, label, grant_binding, current, held, activation, now)
    metadata = {
        "version": 1,
        "observation_hash": observation_hash,
        "ticket": source,
        "observed_at": observation.observed_at,
        "configuration": _configuration(config, project),
        "coverage": observation.coverage,
        "current_events": sorted(
            current,
            key=lambda event: (current[event]["latest"].get("created_at_utc") or "", event),
        ),
        "unknown_intents": list(publication.unknown_intents),
        "binding": binding,
        "label_reason": label.reason,
        "label_repos": label.repos,
        "fixed_notice_episode": _fixed_notice_episode(previous, observation, label),
        "activation": activation,
    }
    if state != "open":
        metadata["reason"] = state
    elif observation.coverage != "complete" or facts.body is None:
        metadata["reason"] = "context_incomplete"
    elif publication.unknown_intents:
        metadata["reason"] = "publication_unknown"
    elif label.reason in ("repository_conflict", "label_facts_unknown"):
        metadata["reason"] = label.reason
    elif binding and binding["state"] == "paused":
        metadata["reason"] = "confirmation_required"
    elif grant_binding != binding:
        metadata["reason"] = "resume_confirmation_unavailable"
    elif activation == "activation_drift":
        metadata["reason"] = activation
    else:
        metadata["reason"] = "observed"
    revision = _observation_revision(metadata, current)
    metadata["revision"] = revision
    _save_source(conn, ticket, facts, observation.observed_at, metadata)
    return IntakeResult(source_id, revision, metadata["reason"], tuple(created), *cancellation)


def _save_source(conn, ticket, facts, observed_at, metadata):
    upsert_tapd_source(
        conn,
        source_id=ticket.source_id,
        source_key=ticket.source_id,
        host=ticket.host,
        project_key=ticket.workspace_id,
        source_type=ticket.kind,
        external_id=ticket.external_id,
        html_url=None,
        title=facts.title or "",
        state=facts.status or "unknown",
        source_updated_at=observed_at,
        metadata=metadata,
    )


def observe_ticket(conn, config, observation, publication, *, now):
    """Persist materialized observations after I/O, without dispatch or external effects."""
    facts = observation.facts
    ticket = facts.identity
    project = _project(config, ticket)
    seen = utc_timestamp(observation.observed_at)
    written = utc_timestamp(now)
    comments = [item.version.event_id for item in observation.comments]
    absences = [item.event_id for item in observation.absences]
    if (
        publication.ticket != ticket
        or seen is None
        or written is None
        or seen > written
        or observation.coverage not in ("incomplete", "unstable", "complete")
        or any(item.version.ticket != ticket for item in observation.comments)
        or any(item.ticket != ticket for item in observation.absences)
        or len(comments) != len(set(comments))
        or len(absences) != len(set(absences))
        or set(comments) & set(absences)
    ):
        raise ValueError("TAPD observation or publication scope is inconsistent")
    source_id = ticket.source_id
    write.lock_observation(conn, source_id)
    previous = _load(conn, ticket)
    observation_hash = authority.digest(
        {
            "snapshot": {
                **asdict(observation),
                "comments": [
                    asdict(comment) for comment in sorted(observation.comments, key=lambda row: row.version.event_id)
                ],
                "absences": [asdict(absence) for absence in sorted(observation.absences, key=lambda row: row.event_id)],
            },
            "publication": {
                "own_comments": sorted(publication.own_comments),
                "unknown_intents": sorted(publication.unknown_intents),
            },
            "configuration": _configuration(config, project),
        }
    )
    latest = utc_timestamp(previous.get("observed_at"))
    if latest is not None:
        if seen < latest:
            return IntakeResult(source_id, None, "stale_observation")
        if seen == latest:
            if previous.get("observation_hash") == observation_hash and previous.get("revision") is not None:
                return IntakeResult(source_id, previous["revision"], previous["reason"])
            # This timestamp cannot order explicit contradictory provider evidence.
            # Invalidate the prior revision and require a later complete observation.
            blocked = {
                **previous,
                "coverage": "unstable",
                "revision": authority.digest(observation_hash),
                "reason": "conflicting_observation",
                "observation_hash": None,
            }
            _save_source(conn, ticket, facts, observation.observed_at, blocked)
            return IntakeResult(source_id, blocked["revision"], "conflicting_observation")
    checkpoint = "observe_tapd_source"
    transactions.savepoint(conn, checkpoint)
    try:
        result = _persist_observation(
            conn,
            config,
            project,
            observation,
            publication,
            now=now,
            previous=previous,
            observation_hash=observation_hash,
        )
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.release(conn, checkpoint)
    return result


def _current(conn, config, ticket, revision):
    metadata = _load(conn, ticket)
    project = _project(config, ticket)
    if (
        not metadata
        or not revision
        or metadata.get("revision") != revision
        or metadata["configuration"] != _configuration(config, project)
        or metadata["coverage"] != "complete"
        or metadata["unknown_intents"]
        or metadata["ticket"]["body"] is None
    ):
        return None
    stored = metadata["ticket"]
    for key in ("owners", "labels", "terminal_states"):
        if stored[key] is not None:
            stored[key] = tuple(stored[key])
    facts = authority.TicketFacts(identity=ticket, **stored)
    all_events = metadata["current_events"]
    current = {
        row["event_id"]: json.loads(row["payload_json"])
        for row in read.events(conn, ticket.source_id, _COMMENT)
        if row["event_id"] in all_events
    }
    if len(current) != len(all_events):
        return None
    binding = metadata.get("binding")
    if source_confirmations.resume_required(conn, ticket.source_id, binding):
        status, _answer = source_confirmations.applied_resume_status(
            conn, config, project, facts, binding, current, complete=True
        )
        if status != "current":
            metadata = {**metadata, "binding": _without_current_resume(binding)}
    return metadata, project, facts, current


def current_label_authority(conn, config, ticket, *, revision):
    """Read the existing active tag binding even when all requests were delivered."""
    if config.get("host") != ticket.host or not any(
        project["workspace_id"] == ticket.workspace_id for project in config["projects"]
    ):
        return authority.AuthorityDecision("source_scope_unavailable")
    loaded = _current(conn, config, ticket, revision)
    if loaded is None:
        return authority.AuthorityDecision("context_unavailable")
    metadata, project, facts, _events = loaded
    binding = metadata.get("binding")
    label = authority.label_authority(facts, project)
    if label.reason != "allowed":
        return label
    if not binding or binding.get("state") != "active" or binding.get("rule") != label.rule:
        return authority.AuthorityDecision(
            binding["reason"]
            if binding and binding.get("reason") == "resume_confirmation_unavailable"
            else "confirmation_required"
        )
    return label


def github_entry_grant(conn, config, source_id, repository):
    """Save an upper bound from the known source at this event's first observation."""
    readonly = {"capability": "readonly", "rule": None}
    tapd = (config or {}).get("tapd") or {}
    source = github_read.source(conn, source_id) if source_id else None
    if not source or tapd.get("enabled") is not True:
        return readonly
    try:
        ticket = authority.TicketIdentity(
            source["host"], source["project_key"], source["source_type"], source["external_id"]
        )
        metadata = json.loads(source["metadata_json"])
        decision = current_label_authority(conn, tapd, ticket, revision=metadata.get("revision"))
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return readonly
    return (
        {"capability": "development", "rule": decision.rule}
        if decision.reason == "allowed" and decision.rule and decision.rule["repo"] == repository
        else readonly
    )


def fixed_notice_episode(conn, config, ticket, *, revision):
    """Read a current authoritative conflict episode, or its proven resolution."""
    loaded = _current(conn, config, ticket, revision)
    if loaded is None:
        return None
    metadata, _project, _facts, _events = loaded
    if metadata["label_reason"] in {"ticket_state_unknown", "label_facts_unknown"}:
        return None
    return metadata.get("fixed_notice_episode")


def _confirmation_context(conn, config, ticket, revision):
    if (
        config.get("enabled") is not True
        or config.get("host") != ticket.host
        or not any(project["workspace_id"] == ticket.workspace_id for project in config["projects"])
    ):
        return None
    return _current(conn, config, ticket, revision)


def paused_confirmation_scope(conn, config, ticket, *, revision):
    """Derive the one supported question from the latest complete tag observation."""
    loaded = _confirmation_context(conn, config, ticket, revision)
    if loaded is None:
        return None
    metadata, project, facts, _events = loaded
    binding = metadata.get("binding")
    if not binding or binding.get("reason") == "resume_confirmation_unavailable":
        return None
    return confirmation_policy.supported_scope(config, project, facts, binding)


def current_confirmation(conn, config, ticket, *, revision, question_id, version, option, scope_digest):
    """Read a currently valid exact published choice without claiming or executing it."""
    loaded = _confirmation_context(conn, config, ticket, revision)
    if loaded is None or type(version) is not int:
        return None
    question = source_confirmation.current(conn, ticket.source_id)
    if question is None or (question["question_id"], question["version"], question["scope_digest"]) != (
        question_id,
        version,
        scope_digest,
    ):
        return None
    metadata, project, facts, events = loaded
    scope = confirmation_policy.supported_scope(config, project, facts, metadata.get("binding"))
    if scope is None or scope["option"] != option or scope != source_confirmations.scope_for_question(question):
        return None
    answer = source_confirmation.answer(conn, question_id)
    return source_confirmations.validated_receipt(
        conn, ticket, question, events.get(answer["event_id"]) if answer is not None else None, project
    )


def claim_confirmation(
    conn, config, ticket, claim, *, revision, question_id, version, option, scope_digest, consumer_id, now
):
    """Revalidate and claim one exact answer in the named consumer's writer transaction."""
    if (
        not conn.in_transaction
        or claim.source_id != ticket.source_id
        or not isinstance(consumer_id, str)
        or not consumer_id.strip()
        or len(consumer_id) > 160
        or not source_sessions.lock_claim(conn, claim, now=now)
    ):
        return None
    receipt = current_confirmation(
        conn,
        config,
        ticket,
        revision=revision,
        question_id=question_id,
        version=version,
        option=option,
        scope_digest=scope_digest,
    )
    if (
        receipt is None
        or receipt.consumer_id is not None
        or not confirmation_store.claim(conn, question_id, consumer_id=consumer_id, now=now)
    ):
        return None
    return replace(receipt, consumer_id=consumer_id)


def claimed_confirmation(conn, config, ticket, *, revision, question_id, version, option, scope_digest, consumer_id):
    """Check a named receipt while its original paused binding remains current.

    A future consumer must link its actual transition and binding request to
    the receipt before using it as evidence of a dependent active grant.
    """
    receipt = current_confirmation(
        conn,
        config,
        ticket,
        revision=revision,
        question_id=question_id,
        version=version,
        option=option,
        scope_digest=scope_digest,
    )
    return receipt if receipt is not None and consumer_id and receipt.consumer_id == consumer_id else None


def _result_delivered(conn, ticket, root, target):
    if source_delivery_recovery.completed(conn, root, target):
        return True
    round_ = source_results.delivery_round(conn, ticket.source_id, root["round_id"])
    return bool(
        round_ is not None
        and round_["status"] in (SourceRoundStatus.EXECUTION_FINISHED, SourceRoundStatus.WAITING_FOR_USER)
        and source_results.confirmed_required_effects(conn, root["source_result_id"], target)
    )


def prior_results_settled(conn, ticket):
    """Verify prior delivery or dormant evidence before unrelated future work."""
    source = source_results.tapd_source(conn, ticket.source_id)
    if source is None:
        return False
    target = source_results.source_target(source)
    return all(
        _result_delivered(conn, ticket, root, target)
        or source_retirement.is_retired(conn, root["source_result_id"])
        or source_delivery_pauses.is_dormant(conn, root["source_result_id"])
        for root in source_confirmation.prior_accepted_roots(conn, ticket.source_id)
    )


def _prior_results_closed(conn, ticket):
    source = source_results.tapd_source(conn, ticket.source_id)
    if source is None:
        return False
    target = source_results.source_target(source)
    return all(
        _result_delivered(conn, ticket, root, target) or source_retirement.is_retired(conn, root["source_result_id"])
        for root in source_confirmation.prior_accepted_roots(conn, ticket.source_id)
    )


def _resume_repository(conn, config, name):
    ids = read.repository_ids(conn, (name,))
    if name not in ids:
        return None
    row = source_tasks.repository(conn, ids[name])
    matches = [repo for repo in config.get("repos", ()) if repo["full_name"] == name]
    if row is None or len(matches) != 1 or matches[0].get("enabled", True) is not True:
        return None
    identity = source_confirmations.repository_identity(row)
    return (
        identity if all(matches[0].get(key) == value for key, value in identity.items() if key != "repo_id") else None
    )


def paused_delivery_scope(conn, config, ticket, members, *, revision):
    """Prove a latched same-rule label loss and each currently intact trigger."""
    if not members:
        return None
    return _paused_delivery_scope(conn, config, ticket, members, revision=revision)


def paused_mixed_delivery_scope(conn, config, ticket, root, round_, checked, *, revision):
    if (
        checked is None
        or checked.revoked
        or tuple(row["request_id"] for row in checked.intact)
        != (source_members.round_github_request_ids(conn, round_["round_id"]))
    ):
        return None
    scope = mixed_same_rule_pause(conn, config, ticket, root, round_, revision=revision)
    return {**scope, "github_disposition": list(checked.intact)} if scope else None


def mixed_same_rule_pause(conn, config, ticket, root, round_, *, revision):
    """Recognize the same paused parent rule independently of discussion availability."""
    if round_["capability"] != "development":
        return None
    members = source_members.disposition_members(conn, root, round_)
    if not members:
        return None
    original = (json.loads(round_["input_snapshot_json"]) or {}).get("authority", {})
    github = tuple(item for item in members if item.ownership.return_target is not None)
    if not github:
        return None
    tapd = tuple(
        {**dict(item.ownership.request), "authorization_json": json.dumps(original[item.member["request_id"]])}
        for item in members
        if item.ownership.return_target is None
    )
    scope = _paused_delivery_scope(conn, config, ticket, tapd, revision=revision)
    if scope is None:
        return None
    rule, repository = scope["binding"]["rule"], scope["repository"]
    for item in github:
        grant = original[item.member["request_id"]]
        if (
            grant.get("capability") != "development"
            or grant.get("rule") != rule
            or grant.get("repos") != [repository["full_name"]]
            or grant.get("entry_grant") != {"capability": "development", "rule": rule}
            or item.ownership.request["repo_id"] != repository["repo_id"]
        ):
            return None
    return scope


def _paused_delivery_scope(conn, config, ticket, members, *, revision):
    loaded = _confirmation_context(conn, config.get("tapd") or {}, ticket, revision)
    if loaded is None:
        return None
    metadata, project, facts, current = loaded
    binding = metadata.get("binding")
    rule = binding.get("rule") if binding else None
    label = authority.label_authority(facts, project)
    if (
        not isinstance(rule, dict)
        or binding.get("state") != "paused"
        or binding.get("reason") != "no_label_match"
        or label.reason not in ("no_label_match", "allowed")
        or (label.reason == "allowed" and label.rule != rule)
        or rule["repo"] not in project["repositories"]
        or (
            binding.get("resume_transition_id") is not None
            and not source_confirmations.applied_answer_intact(conn, ticket, binding, project, current)
        )
        or not any(
            ticket.kind in item["kinds"] and all(item.get(key) == value for key, value in rule.items())
            for item in project["rules"]
        )
    ):
        return None
    repository = _resume_repository(conn, config, rule["repo"])
    original = {"old_rule": rule, "binding_request_id": binding["request_id"]}
    if repository is None or not source_confirmations.original_binding_owned(conn, ticket, original, repository):
        return None
    if any(
        not source_confirmations.same_rule_request(row, original, repository)
        or _current_request_decision(loaded, row, delivery=True).reason != "confirmation_required"
        for row in members
    ):
        return None
    return {"binding": binding, "repository": repository}


@dataclass(frozen=True, slots=True)
class ResumeOffer:
    metadata: dict
    project: dict
    facts: authority.TicketFacts
    comments: dict
    scope: dict
    arguments: dict
    receipt: source_confirmations.ConfirmationReceipt
    repository: dict


def same_rule_resume_offer(conn, config, ticket, *, revision):
    """Read the current published same-rule offer and exact unclaimed answer."""
    tapd = config.get("tapd") or {}
    loaded = _confirmation_context(conn, tapd, ticket, revision)
    question = source_confirmation.current(conn, ticket.source_id)
    if loaded is None or question is None:
        return None
    metadata, project, facts, current = loaded
    paused = metadata.get("binding")
    scope = confirmation_policy.supported_scope(tapd, project, facts, paused)
    if (
        scope is None
        or paused.get("reason") == "resume_confirmation_unavailable"
        or scope["option"] != "resume"
        or scope["current_rule"] != scope["old_rule"]
        or scope != source_confirmations.scope_for_question(question)
    ):
        return None
    arguments = {
        "revision": revision,
        "question_id": question["question_id"],
        "version": question["version"],
        "option": "resume",
        "scope_digest": question["scope_digest"],
    }
    receipt = current_confirmation(conn, tapd, ticket, **arguments)
    if receipt is None or receipt.consumer_id is not None:
        return None
    repository = _resume_repository(conn, config, scope["old_rule"]["repo"])
    if repository is None or not source_confirmations.original_binding_owned(conn, ticket, scope, repository):
        return None
    return ResumeOffer(metadata, project, facts, current, scope, arguments, receipt, repository)


def current_delivery_transition(conn, config, ticket, *, revision):
    """Return the exact applied transition that still authorizes this ticket."""
    loaded = _confirmation_context(conn, config.get("tapd") or {}, ticket, revision)
    binding = loaded[0].get("binding") if loaded else None
    return binding.get("resume_transition_id") if binding and binding.get("state") == "active" else None


def apply_paused_resume(conn, config, ticket, claim, *, revision, now):
    """Apply one exact choice to preaccepted work following settled original delivery."""
    if not _prior_results_closed(conn, ticket) or read_sessions.blockers_for_source(conn, ticket.source_id, limit=1):
        return None
    return _apply_same_rule_resume(conn, config, ticket, claim, revision=revision, now=now)


def apply_delivery_resume(conn, config, ticket, claim, *, source_result_id, generation, revision, now):
    """Apply the exact choice belonging to a newly staged durable delivery owner."""
    owner = source_delivery_recovery.active(conn, ticket.source_id)
    offer = same_rule_resume_offer(conn, config, ticket, revision=revision)
    if (
        owner is None
        or offer is None
        or owner["source_result_id"] != source_result_id
        or owner["generation"] != generation
        or owner["question_id"] != offer.receipt.question_id
        or owner["claimed_at"] != now
        or source_delivery_recovery.current_bound(conn, owner) is None
        or owner["transition_id"]
        != source_confirmations.resume_identity(
            ticket.source_id, offer.receipt.question_id, offer.receipt.version, offer.receipt.scope_digest
        )
    ):
        return None
    return _apply_same_rule_resume(conn, config, ticket, claim, revision=revision, now=now)


def _apply_same_rule_resume(conn, config, ticket, claim, *, revision, now):
    tapd = config.get("tapd") or {}
    if (
        not conn.in_transaction
        or claim.source_id != ticket.source_id
        or not source_sessions.lock_claim(conn, claim, now=now)
        or read_sessions.active_round(conn, ticket.source_id) is not None
    ):
        return None
    offer = same_rule_resume_offer(conn, config, ticket, revision=revision)
    if offer is None:
        return None
    metadata, project, facts, current = offer.metadata, offer.project, offer.facts, offer.comments
    scope, arguments, receipt, repository = offer.scope, offer.arguments, offer.receipt, offer.repository
    paused = metadata["binding"]
    transition_id = source_confirmations.resume_identity(
        ticket.source_id, receipt.question_id, receipt.version, receipt.scope_digest
    )
    binding = source_confirmations.resumed_binding(paused, transition_id)
    label = authority.label_authority(facts, project)
    restore = []
    for row in read.pending_requests(conn, ticket.source_id):
        if row["phase"] != Phase.PAUSED or not source_confirmations.same_rule_request(row, scope, repository):
            continue
        decision = _request_decision(
            row,
            facts=facts,
            project=project,
            label=label,
            binding=binding,
            current=current,
            activation=metadata["activation"],
        )
        if decision.reason == "allowed" and decision.capability == label.capability and decision.repos == label.repos:
            restore.append(row["request_id"])

    checkpoint = "apply_source_resume"
    transactions.savepoint(conn, checkpoint)
    try:
        claimed = claim_confirmation(conn, tapd, ticket, claim, **arguments, consumer_id=transition_id, now=now)
        if claimed is None:
            transactions.release(conn, checkpoint)
            return None
        confirmation_store.record_resume(
            conn,
            source_id=ticket.source_id,
            transition_id=transition_id,
            receipt=asdict(claimed),
            binding=paused,
            repository=repository,
            request_ids=restore,
            now=now,
        )
        for request_id in restore:
            if not write.amend_request(
                conn, source_id=ticket.source_id, request_id=request_id, phase=Phase.PENDING.value, now=now
            ):
                raise ValueError("Paused source request changed before resume")
        _refresh_requests(conn, ticket, facts, project, label, binding, current, False, metadata["activation"], now)
        metadata = {**metadata, "binding": binding, "reason": "observed"}
        metadata["revision"] = _observation_revision(metadata, current)
        _save_source(conn, ticket, facts, metadata["observed_at"], metadata)
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.release(conn, checkpoint)
    return IntakeResult(ticket.source_id, metadata["revision"], "observed")


def revalidate_request(conn, config, ticket, request_id, *, revision):
    """Revalidate one request against a named, freshly observed source revision."""
    loaded = _current(conn, config, ticket, revision)
    row = records.first(read.pending_requests(conn, ticket.source_id, request_id=request_id))
    if loaded is None or row is None or row["source_id"] != ticket.source_id or row["phase"] != Phase.PENDING:
        return authority.AuthorityDecision("request_or_context_unavailable")
    return _current_request_decision(loaded, row)


def _current_request_decision(loaded, row, *, delivery=False):
    metadata, project, facts, current = loaded
    return _request_decision(
        row,
        facts=facts,
        project=project,
        label=authority.label_authority(facts, project),
        binding=metadata["binding"],
        current=current,
        activation=metadata["activation"],
        delivery=delivery,
    )


def delivery_request_decisions(conn, config, ticket, rows, *, revision):
    """Evaluate current policy for grants proved by the caller's consumed root lineage."""
    loaded = _current(conn, config, ticket, revision)
    if loaded is None:
        return ()
    return tuple(_current_request_decision(loaded, row, delivery=True) for row in rows)


def current_accepted_github_delivery(conn, config, ticket, proof, *, now):
    """Re-evaluate a dedicated accepted-root proof without editing original member state."""
    if not isinstance(proof, source_github_observation.RootDeliveryProof):
        return None
    return _current_github_root(conn, config, ticket, proof, now=now)


def current_accepted_github_completion(conn, config, ticket, proof, *, now):
    """Recheck the dedicated completion operations; no new effect authorization."""
    if not isinstance(proof, source_github_observation.RootCompletionProof):
        return None
    return _current_github_root(conn, config, ticket, proof, now=now)


def _current_github_root(conn, config, ticket, proof, *, now):
    loaded = _current(conn, config["tapd"], ticket, proof.revision)
    if loaded is None:
        return None
    metadata, project, facts, _comments = loaded
    return source_request_authority.delivery_current_grants(
        conn, config, ticket, proof, facts=facts, project=project, binding=metadata["binding"], now=now
    )


def completion_tapd_cancellation(conn, config, ticket, members, decisions, *, revision):
    """Distinguish known original grant loss from unavailable current context."""
    permanent = permanent_delivery_cause(conn, config, ticket, members, revision=revision)
    if permanent:
        return permanent["reason"]
    return next((decision.reason for decision in decisions if decision.reason in _CANCELLING), None)


def permanent_delivery_cause(conn, config, ticket, members, *, revision):
    """Prove a permanent loss of an original TAPD grant from a complete observation."""
    return _permanent_delivery_cause(conn, config, ticket, members, revision=revision) if members else None


def mixed_permanent_tapd_cause(conn, config, ticket, root, round_, *, revision):
    """Use exact historical original membership, including unhandled TAPD requests."""
    members = source_members.disposition_members(conn, root, round_)
    if not members:
        return None
    snapshot = json.loads(round_["input_snapshot_json"])
    original = tuple(
        {
            **dict(item.ownership.request),
            "authorization_json": json.dumps(snapshot["authority"][item.member["request_id"]]),
        }
        for item in members
        if item.ownership.return_target is None
    )
    return _permanent_delivery_cause(conn, config, ticket, original, revision=revision)


def _permanent_delivery_cause(conn, config, ticket, members, *, revision):
    for member in members:
        request = read_requests.find_by_id(conn, member["request_id"])
        if request is None or request["provider"] != "tapd" or request["source_id"] != ticket.source_id:
            return None
    loaded = _confirmation_context(conn, config, ticket, revision)
    if loaded is None:
        return None
    metadata, project, facts, comments = loaded
    if authority.ticket_state(facts) == "terminal":
        return {
            "reason": "terminal",
            "archived": facts.archived,
            "status": facts.status,
            "terminal_states": list(facts.terminal_states or ()),
            "observation_hash": metadata["observation_hash"],
        }
    for member in members:
        if member["auth_type"] != Auth.TAPD_MENTION:
            continue
        event_id = json.loads(member["authorization_json"]).get("event_id")
        event = comments.get(event_id)
        if not event or event.get("disposition") != "revoked":
            continue
        first, latest = event["first"], event["latest"]
        if latest.get("deleted"):
            reason = "comment_deleted"
            version = latest["deleted"]
        elif (
            latest.get("structure_verified") is True
            and latest.get("mentions_bot") is False
            and latest.get("creator") == first.get("creator") == project["trusted_author"]
            and latest.get("creator_evidence")
            and latest.get("own_body_evidence")
            and all(latest.get(key) == first.get(key) for key in ("bot_name", "reply_name", "normalization_version"))
            and latest.get("body_sha256") != first.get("body_sha256")
            and not latest.get("bot_owned")
            and not latest.get("bot_prefix")
        ):
            reason = "mention_removed"
            version = latest["body_sha256"]
        else:
            continue
        return {
            "reason": reason,
            "request_id": member["request_id"],
            "event_id": event_id,
            "version": version,
            "observation_hash": metadata["observation_hash"],
        }
    return None


def round_input_status(conn, round_record, *, revision=None):
    """Return current, changed, frozen or unavailable without changing the input packet."""
    return _round_input_status(conn, round_record, revision=revision)


def _round_input_status(conn, round_record, *, revision, github_grants=()):
    launched = read_sessions.round_launch_state(conn, round_record["round_id"])
    if launched == "unknown":
        return "unavailable"
    if launched == "started":
        return "frozen"
    source_id = round_record["source_id"]
    try:
        observed = read.source_observation(conn, source_id)
        metadata = json.loads(observed[0]) if observed else {}
        if (
            not metadata.get("revision")
            or (revision is not None and metadata["revision"] != revision)
            or metadata.get("coverage") != "complete"
            or metadata.get("unknown_intents")
            or metadata["ticket"]["body"] is None
        ):
            return "unavailable"
        frozen = json.loads(round_record["input_snapshot_json"])
        members = source_sessions.pending_round_members(conn, round_record["round_id"])
        if not members:
            return "unavailable"
        frozen_requests = {item["request_id"]: (item["content_version"], item["input"]) for item in frozen["requests"]}
        if len(frozen_requests) != len(frozen["requests"]) or frozen_requests != {
            row["request_id"]: (row["content_version"], json.loads(row["content_snapshot_json"])) for row in members
        }:
            return "unavailable"
        pending = {row["request_id"]: row for row in read.pending_requests(conn, source_id)}
        pending.update((grant.request["request_id"], grant.request) for grant in github_grants)
        newest = [
            {
                "request_id": member["request_id"],
                "input": json.loads(pending[member["request_id"]]["content_snapshot_json"]),
            }
            for member in members
        ]
        events = {row["event_id"]: json.loads(row["payload_json"]) for row in read.events(conn, source_id, _COMMENT)}
        discussion = [{**events[event]["latest"], "event_id": event} for event in metadata["current_events"]]
        if frozen["configuration"] != metadata["configuration"]:
            return "changed"
        return (
            "current"
            if _semantic_input(frozen["ticket"], frozen["discussion"], frozen["requests"])
            == _semantic_input(metadata["ticket"], discussion, newest)
            else "changed"
        )
    except (KeyError, TypeError, ValueError, RecursionError):
        return "unavailable"


def admit_round(conn, config, ticket, claim, *, round_id, revision, now, configured_repositories=None):
    """Freeze one oldest compatible batch from a complete current observation."""
    if ticket.source_id != claim.source_id or not source_sessions.lock_claim(conn, claim, now=now):
        return None
    if read_sessions.active_round(conn, ticket.source_id) is not None:
        return None
    loaded = _current(conn, config, ticket, revision)
    if loaded is None:
        return None
    return _admit_current(
        conn,
        ticket,
        claim,
        round_id=round_id,
        revision=revision,
        now=now,
        loaded=loaded,
        configured_repositories=configured_repositories,
    )


def admit_mixed_round(conn, config, ticket, claim, *, round_id, revision, now, operations):
    """Apply exact current GitHub operations and freeze a compatible source round."""
    if (
        ticket.source_id != claim.source_id
        or not source_sessions.lock_claim(conn, claim, now=now)
        or read_sessions.active_round(conn, ticket.source_id) is not None
        or read_sessions.blockers_for_source(conn, ticket.source_id, limit=1)
    ):
        return None, False
    loaded = _current(conn, config["tapd"], ticket, revision)
    if loaded is None:
        return None, False
    metadata, project, facts, _ = loaded
    checkpoint = "apply_mixed_source_requests"
    transactions.savepoint(conn, checkpoint)
    try:
        grants = source_request_authority.promote_observed_requests(
            conn,
            config,
            ticket,
            claim,
            revision=revision,
            operations=operations,
            facts=facts,
            project=project,
            binding=metadata["binding"],
            now=now,
        )
        if grants is None:
            transactions.finish_isolation(conn, checkpoint, rollback=True)
            return None, False
        round_ = _admit_current(
            conn,
            ticket,
            claim,
            round_id=round_id,
            revision=revision,
            now=now,
            loaded=loaded,
            configured_repositories=config["repos"],
            github_grants=grants,
        )
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.release(conn, checkpoint)
    return round_, True


def _admit_current(
    conn,
    ticket,
    claim,
    *,
    round_id,
    revision,
    now,
    loaded,
    configured_repositories,
    prior_ids=None,
    previous_scope=None,
    github_grants=(),
):
    metadata, project, facts, current = loaded
    label = authority.label_authority(facts, project)
    if label.reason == "repository_conflict":
        return None
    pending = read.pending_requests(conn, ticket.source_id)
    allowed = []
    for row in pending:
        if row["phase"] != Phase.PENDING:
            continue
        decision = _request_decision(
            row,
            facts=facts,
            project=project,
            label=label,
            binding=metadata["binding"],
            current=current,
            activation=metadata["activation"],
        )
        if decision.capability is not None:
            allowed.append((row, decision))
    allowed.extend((grant.request, grant.decision) for grant in github_grants)
    if github_grants:
        allowed.sort(key=lambda pair: (pair[0]["created_at"], pair[0]["request_id"]))
    if not allowed:
        return None
    if prior_ids is not None:
        available = {row["request_id"]: (row, grant) for row, grant in allowed}
        if not all(request_id in available for request_id in prior_ids):
            return None
        allowed = [available[request_id] for request_id in prior_ids]
    initial = next((pair for pair in allowed if pair[0]["auth_type"] == Auth.LABEL_QUEUE), None)
    if initial is not None:
        root_request, decision = initial
        grouped = [root_request]
    else:
        _, decision = allowed[0]
        grouped = [
            row
            for row, authority in allowed
            if (authority.capability, authority.repos) == (decision.capability, decision.repos)
        ]
    if prior_ids is not None and tuple(row["request_id"] for row in grouped) != prior_ids:
        return None
    ceiling, repos = decision.capability, decision.repos
    allowed_repos = (
        set(repos)
        if configured_repositories is None
        else {
            repo["full_name"]
            for repo in configured_repositories
            if repo.get("enabled", True) is True and repo["full_name"] in repos
        }
    )
    if not allowed_repos or (ceiling == "development" and set(repos) != allowed_repos):
        return None
    ids = read.repository_ids(conn, allowed_repos)
    if not ids or len(ids) != len(allowed_repos):
        return None
    if previous_scope is not None and (ceiling, frozenset(ids.values())) != previous_scope:
        return None
    publication_scope = {
        "provider": "tapd",
        "host": ticket.host,
        "workspace_id": ticket.workspace_id,
        "kind": ticket.kind,
        "external_id": ticket.external_id,
        "action": "comment",
    }
    frozen_requests = {row["request_id"]: json.loads(row["authorization_json"]) for row in grouped}
    scopes = {}
    for row in pending:
        saved = json.loads(row["authorization_json"])
        event_id = saved.get("event_id")
        if event_id is not None:
            scope = "current_request" if row["request_id"] in frozen_requests else "future_request"
            scopes[event_id] = (scope, row["request_id"])
    discussion = []
    for event in metadata["current_events"]:
        data = current[event]
        scope, request_id = scopes.get(event, ("context", None))
        if (
            initial is not None
            and data["disposition"] == "absorbed"
            and authority.comment_authority(data["latest"], project) == "allowed"
        ):
            scope, request_id = "initial_history", initial[0]["request_id"]
        elif data["disposition"] in ("eligible", "after_boundary") and scope == "context":
            scope = "future_request"
        discussion.append(
            {
                **data["latest"],
                "event_id": event,
                "request_scope": scope,
                "request_id": request_id,
            }
        )
    snapshot = {
        "ticket": metadata["ticket"],
        "discussion": discussion,
        "authority": frozen_requests,
        "requests": [
            {
                "request_id": row["request_id"],
                "content_version": row["content_version"],
                "input": json.loads(row["content_snapshot_json"]),
            }
            for row in grouped
        ],
        "configuration": metadata["configuration"],
        "revision": revision,
    }
    return source_sessions.start_round(
        conn,
        claim,
        round_id=round_id,
        member_versions={row["request_id"]: row["content_version"] for row in grouped},
        input_version=revision,
        input_snapshot=snapshot,
        capability=ceiling,
        repo_ids=tuple(ids.values()),
        publication_scope=publication_scope,
        now=now,
    )


@dataclass(frozen=True, slots=True)
class MixedRoundRefresh:
    status: str
    round_id: str
    reason: str | None = None
    worker_proof: source_github_observation.MixedWorkerProof | None = None
    model_proof: source_github_observation.MixedModelProof | None = None


def refresh_mixed_round(conn, config, ticket, claim, *, active, revision, operations, replacement_id, now, target=None):
    """Refresh one active round and bind complete evidence to its named Worker check."""
    round_id = active.round_id
    refused = MixedRoundRefresh("held", round_id, "github_observation_scope_changed")
    round_ = source_sessions.lock_round(conn, claim, round_id, now=now)
    if (
        round_ is None
        or replacement_id == round_id
        or claim.source_id != ticket.source_id
        or source_github_observation.active_scope(conn, round_) != active
    ):
        return refused
    loaded = _current(conn, config["tapd"], ticket, revision)
    if loaded is None:
        return refused
    metadata, project, facts, _comments = loaded
    checkpoint = "refresh_mixed_source_round"
    transactions.savepoint(conn, checkpoint)
    try:
        refreshed = source_request_authority.refresh_active_requests(
            conn,
            config,
            ticket,
            claim,
            revision=revision,
            active=active,
            operations=operations,
            facts=facts,
            project=project,
            binding=metadata["binding"],
            now=now,
            target=target,
        )
        if refreshed is None:
            transactions.finish_isolation(conn, checkpoint, rollback=True)
            return refused
        if refreshed.cancellation is not None:
            if not write.request_cancellation(
                conn, source_id=ticket.source_id, round_id=round_id, reason=refreshed.cancellation, now=now
            ):
                transactions.finish_isolation(conn, checkpoint, rollback=True)
                return refused
            result = MixedRoundRefresh("cancel_requested", round_id, refreshed.cancellation)
        elif refreshed.unavailable:
            result = MixedRoundRefresh("held", round_id, "github_observation_unavailable")
        else:
            grants = refreshed.grants
            if not _same_frozen_github_authority(round_, grants):
                transactions.finish_isolation(conn, checkpoint, rollback=True)
                return MixedRoundRefresh("held", round_id, "source_authority_unavailable")
            freshness = _round_input_status(conn, round_, revision=revision, github_grants=grants)
            if freshness == "changed" and target is not None:
                result = MixedRoundRefresh("held", round_id, "native_input_changed")
            elif freshness == "changed":
                replaced = _replace_round(
                    conn,
                    config,
                    ticket,
                    claim,
                    round_,
                    replacement_id=replacement_id,
                    revision=revision,
                    now=now,
                    github_grants=grants,
                )
                result = (
                    MixedRoundRefresh("readmitted", replaced["round_id"])
                    if replaced
                    else MixedRoundRefresh("held", round_id, "source_input_retirement_unavailable")
                )
            elif freshness in ("frozen", "current"):
                if isinstance(target, source_github_observation.ModelObservationTarget):
                    proof = source_github_observation.MixedModelProof(
                        active, claim, revision, tuple(operations), target
                    )
                    result = MixedRoundRefresh("current", round_id, model_proof=proof)
                else:
                    proof = source_github_observation.MixedWorkerProof(
                        active, claim, revision, tuple(operations), target
                    )
                    result = MixedRoundRefresh("current", round_id, worker_proof=proof)
            else:
                result = MixedRoundRefresh("held", round_id, "source_input_unavailable")
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.release(conn, checkpoint)
    return result


def _same_frozen_github_authority(round_, grants):
    frozen = json.loads(round_["input_snapshot_json"]).get("authority")
    return isinstance(frozen, dict) and all(
        grant.decision.capability == round_["capability"]
        and frozen.get(grant.request["request_id"]) == json.loads(grant.request["authorization_json"])
        for grant in grants
    )


def current_mixed_worker_grants(conn, config, ticket, claim, round_, *, revision, proof, now, target=None):
    """Verify exact sealed operations and frozen input for one in-flight Worker check."""
    if not isinstance(proof, source_github_observation.MixedWorkerProof):
        return None
    return _current_mixed_execution_grants(conn, config, ticket, claim, round_, revision, proof, now, target)


def current_mixed_model_grants(conn, config, ticket, claim, round_, *, revision, proof, now, target):
    """Verify exact sealed operations and frozen input for one source model check."""
    if not isinstance(proof, source_github_observation.MixedModelProof):
        return None
    return _current_mixed_execution_grants(conn, config, ticket, claim, round_, revision, proof, now, target)


def _current_mixed_execution_grants(conn, config, ticket, claim, round_, revision, proof, now, target):
    if (
        proof.claim != claim
        or proof.revision != revision
        or proof.target != target
        or source_github_observation.active_scope(conn, round_) != proof.active
    ):
        return None
    loaded = _current(conn, config["tapd"], ticket, revision)
    if loaded is None:
        return None
    metadata, project, facts, _ = loaded
    grants = source_request_authority.current_execution_grants(
        conn,
        config,
        ticket,
        proof,
        facts=facts,
        project=project,
        binding=metadata["binding"],
        now=now,
    )
    if (
        grants is None
        or not _same_frozen_github_authority(round_, grants)
        or _round_input_status(
            conn,
            round_,
            revision=revision,
            github_grants=grants,
        )
        not in ("current", "frozen")
    ):
        return None
    return {grant.request["request_id"]: grant.decision for grant in grants}


def _refreeze_members(
    conn, tapd, ticket, claim, old_round, *, round_id, revision, now, configured_repositories, github_grants=()
):
    members = source_sessions.pending_round_members(conn, old_round["round_id"])
    loaded = _current(conn, tapd, ticket, revision)
    if not members or loaded is None:
        return None
    original = json.loads(old_round["input_snapshot_json"])
    ordered_ids = tuple(row["request_id"] for row in original["requests"])
    if set(ordered_ids) != {row["request_id"] for row in members} or len(ordered_ids) != len(members):
        return None
    return _admit_current(
        conn,
        ticket,
        claim,
        round_id=round_id,
        revision=revision,
        now=now,
        loaded=loaded,
        configured_repositories=configured_repositories,
        prior_ids=ordered_ids,
        previous_scope=(old_round["capability"], frozenset(json.loads(old_round["repo_ids_json"]))),
        github_grants=github_grants,
    )


def replace_unlaunched_round(conn, config, ticket, claim, *, round_id, replacement_id, revision, now):
    """Retire and atomically refreeze the original batch with its latest authorized input."""
    if not (config or {}).get("tapd", {}).get("enabled") or ticket.source_id != claim.source_id:
        return None
    if round_id == replacement_id:
        return None
    active = source_sessions.lock_round(conn, claim, round_id, now=now)
    if active is None or round_input_status(conn, active, revision=revision) != "changed":
        return None
    return _replace_round(
        conn, config, ticket, claim, active, replacement_id=replacement_id, revision=revision, now=now
    )


def _replace_round(conn, config, ticket, claim, active, *, replacement_id, revision, now, github_grants=()):
    round_id = active["round_id"]
    if read_sessions.round_launch_state(conn, round_id) != "unlaunched" or read_sessions.round_has_results_or_effects(
        conn, round_id
    ):
        return None
    checkpoint = "replace_unlaunched_round"
    transactions.savepoint(conn, checkpoint)
    try:
        if _retire_children(conn, claim, round_id, now=now):
            replaced = _refreeze_members(
                conn,
                config["tapd"],
                ticket,
                claim,
                active,
                round_id=replacement_id,
                revision=revision,
                now=now,
                configured_repositories=config["repos"],
                github_grants=github_grants,
            )
        else:
            replaced = None
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.finish_isolation(conn, checkpoint, rollback=replaced is None)
    return replaced


def _retire_children(conn, claim, round_id, *, now):
    for child in read_sessions.round_tasks(conn, round_id):
        if not source_sessions.retire_prepared_child(conn, child["task_id"], now=now):
            return False
        ownership.close_terminal_task(conn, child["task_id"], now)
    for model in read_sessions.model_runs(conn, round_id):
        if model["status"] == ModelRunStatus.INTERRUPTED:
            continue
        if not source_model.finish(conn, claim, model["run_id"], outcome=ModelRunStatus.INTERRUPTED, now=now):
            return False
    return source_sessions.finish_round(conn, claim, round_id=round_id, outcome=SourceRoundStatus.CANCELED, now=now)
