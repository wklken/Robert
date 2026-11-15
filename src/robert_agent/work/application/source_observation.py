"""Discover, refresh and safely hold durable TAPD sources."""

from contextlib import closing
from dataclasses import dataclass, replace
import os
import time

from robert_agent.adapters.tapd.client import ReadBudget, TapdClient, TapdError
from robert_agent.foundation import values
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import source_observations as read
from robert_agent.persistence.queries import source_members, source_results, source_sessions as sessions
from robert_agent.persistence.stores import source_observations as write
from robert_agent.persistence.stores import source_sessions, tapd_intake as intake_store
from robert_agent.work import tapd_authority as facts
from robert_agent.work.application import admission, source_github_observation, tapd_intake


@dataclass(frozen=True, slots=True)
class ScanLimits:
    probes: int = 4
    tickets: int = 20
    max_pages: int = 100
    max_scan_bytes: int = 16 * 1024 * 1024
    seconds: int = 120

    def __post_init__(self):
        if any(
            type(value) is not int or value < 1
            for value in (
                self.probes,
                self.tickets,
                self.max_pages,
                self.max_scan_bytes,
                self.seconds,
            )
        ):
            raise ValueError("TAPD scan limits must be positive integers")

    def pages(self, budget, stop):
        return {
            "max_pages": self.max_pages,
            "max_scan_bytes": self.max_scan_bytes,
            "scan_timeout": budget.remaining(),
            "stop": stop,
        }


@dataclass(frozen=True, slots=True)
class ScanAttention:
    key: str
    reason: str


@dataclass(frozen=True, slots=True)
class ScanOutcome:
    status: str
    probes_completed: int = 0
    candidates: int = 0
    tickets_checked: int = 0
    paused_sources: int = 0
    requests: int = 0
    attention: tuple[ScanAttention, ...] = ()


@dataclass(frozen=True, slots=True)
class _Probe:
    key: str
    workspace_id: str
    kind: str | None = None
    owner: str | None = None
    label: str | None = None
    display_author: str | None = None


def _tapd(config):
    return (config or {}).get("tapd") or {}


def _projects(tapd):
    return {(tapd["host"], project["workspace_id"]): project for project in tapd.get("projects", ())}


def _ticket(row):
    ticket = facts.TicketIdentity(row["host"], row["project_key"], row["source_type"], row["external_id"])
    if ticket.source_id != row["source_id"]:
        raise ValueError("TAPD source ID differs from its canonical coordinates")
    return ticket


def _probes(tapd, projects):
    serialized = {}
    for (_, workspace_id), project in projects.items():
        for rule in project["rules"]:
            for kind in rule["kinds"]:
                key = facts.digest((tapd["host"], workspace_id, kind, rule["assignee"], rule["label"]))
                serialized[key] = _Probe(key, workspace_id, kind, rule["assignee"], rule["label"])
        if project["mentions"]["enabled"]:
            author = project["trusted_author"]
            key = facts.digest((tapd["host"], workspace_id, "display-author", author))
            serialized[key] = _Probe(key, workspace_id, display_author=author)
    return serialized


def _make_client(tapd, client_factory):
    return client_factory(
        executable=tapd["executable"],
        cwd=tapd["cwd"],
        environment=os.environ,
    )


def _running(budget, stop):
    if stop is not None and stop.is_set():
        raise TapdError("cancelled")
    return budget.remaining()


def _ordered_probes(conn, probes):
    saved = read.discovery_progress(conn, list(probes))
    return sorted(
        probes.values(), key=lambda probe: (saved[probe.key]["attempted_at"] if probe.key in saved else "", probe.key)
    )


def _candidate_tickets(client, host, probe, *, budget, limits, stop):
    options = limits.pages(budget, stop)
    if probe.display_author is not None:
        scanned = client.discover_comments(probe.workspace_id, probe.display_author, **options)
    else:
        scanned = client.list_items(probe.workspace_id, probe.kind, owner=probe.owner, label=probe.label, **options)
    if scanned.complete is not True:
        raise TapdError("incomplete_discovery")
    tickets = set()
    for item in scanned.items:
        if item.workspace_id != probe.workspace_id or (probe.kind is not None and item.kind != probe.kind):
            raise TapdError("invalid_response")
        external_id = item.entry_id if probe.display_author is not None else item.external_id
        tickets.add(facts.TicketIdentity(host, probe.workspace_id, item.kind, external_id))
    return tickets


def _register(conn, ticket):
    write.ensure_candidate(
        conn,
        source_id=ticket.source_id,
        host=ticket.host,
        workspace_id=ticket.workspace_id,
        kind=ticket.kind,
        external_id=ticket.external_id,
    )
    if read.source(conn, ticket.source_id) is None:
        raise ValueError("TAPD candidate identity collision")


def publication_facts(conn, ticket):
    """Read exact local publication evidence inside the observation or effect writer."""
    saved = source_results.tapd_publication_evidence(conn, ticket.source_id)
    return facts.PublicationFacts(ticket, saved.own_comments, saved.unknown_intents)


def _facts_from_cli(client, ticket, *, budget, limits, stop):
    item = client.get_item(ticket.workspace_id, ticket.kind, ticket.external_id, stop=stop, timeout=budget.remaining())
    comments = client.list_comments(ticket.workspace_id, ticket.kind, ticket.external_id, **limits.pages(budget, stop))
    workflow = client.terminal_states(
        ticket.workspace_id,
        ticket.kind,
        workitem_type_id=item.workitem_type_id,
        stop=stop,
        timeout=budget.remaining(),
    )
    return facts.TicketObservation(
        replace(facts.TicketFacts.from_cli(ticket, item), terminal_states=tuple(sorted(workflow))),
        "",
        tuple(facts.CommentObservation.from_cli(ticket, comment) for comment in comments.items),
    )


def _read_ticket(client, ticket, project, provider, *, budget, limits, stop):
    _running(budget, stop)
    observed_at = values.utc_now()
    if provider is None:
        snapshot = _facts_from_cli(client, ticket, budget=budget, limits=limits, stop=stop)
    else:
        snapshot = provider(client, ticket, project=project, budget=budget, limits=limits, stop=stop)
    _running(budget, stop)
    if not isinstance(snapshot, facts.TicketObservation) or snapshot.facts.identity != ticket:
        raise TapdError("invalid_response")
    # Timestamp the invocation start here. The typed collaborator cannot
    # upgrade an old observation to a newer ordering supplied by the caller.
    started, supplied, ended = map(utc_timestamp, (observed_at, snapshot.observed_at, values.utc_now()))
    if provider is not None and (supplied is None or not started <= supplied <= ended):
        raise TapdError("stale_observation")
    return replace(snapshot, observed_at=observed_at)


def read_ticket_snapshot(client, ticket, project, provider, *, limits=ScanLimits(), stop=None):
    """Read an exact ticket within the controller's observation time bracket."""
    budget = ReadBudget(time.monotonic() + limits.seconds)
    return _read_ticket(client, ticket, project, provider, budget=budget, limits=limits, stop=stop)


def _removed_scope(config, project, source):
    if _tapd(config).get("enabled") is not True:
        return "source_disabled"
    if project is None:
        return "source_project_removed"
    allowed = {repo["full_name"] for repo in config.get("repos", ()) if repo.get("enabled", True)}
    selected = set(project["repositories"]) & allowed
    if not selected:
        return "source_repository_removed"
    binding = values.json_object(source["metadata_json"]).get("binding") or {}
    if binding.get("state") in {"active", "paused"}:
        previous = binding["rule"]
        names = ("id", "assignee", "label", "repo")
        if previous["repo"] not in selected or not any(
            source["source_type"] in rule["kinds"] and {key: rule[key] for key in names} == previous
            for rule in project["rules"]
        ):
            return "source_rule_removed"
    return None


def removed_scope(config, ticket, persisted_source):
    """Report a durable local scope removal before a provider read."""
    return _removed_scope(config, _projects(_tapd(config)).get((ticket.host, ticket.workspace_id)), persisted_source)


def _hold(conn, row, reason, now, *, pause):
    metadata = values.json_object(row["metadata_json"])
    write.hold_source(conn, row["source_id"], metadata, reason, now, pause=pause)
    if pause and (active := sessions.active_round(conn, row["source_id"])) is not None:
        intake_store.request_cancellation(
            conn,
            source_id=row["source_id"],
            round_id=active["round_id"],
            reason=reason,
            now=now,
        )


def _refresh_candidate(db_path, config, row, project, client, provider, *, budget, limits, stop):
    ticket = _ticket(row)
    started = values.utc_now()
    with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
        transactions.begin_immediate(conn)
        write.observation_checkpoint(conn, ticket.source_id, started)
    observation = None
    reason = None
    try:
        observation = _read_ticket(client, ticket, project, provider, budget=budget, limits=limits, stop=stop)
    except TapdError as exc:
        reason = exc.code
    except (OSError, ValueError, TypeError):
        reason = "observation_unavailable"
    now = values.utc_now()
    with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
        transactions.begin_immediate(conn)
        current = read.source(conn, ticket.source_id)
        progress = read.observation_progress(conn, ticket.source_id)
        if current is None or progress is None or progress["attempted_at"] != started:
            return 0, "stale_observation"
        latest = utc_timestamp(current["source_updated_at"])
        if latest is not None and latest >= utc_timestamp(started):
            return 0, "stale_observation"
        removed = _removed_scope(config, project, current)
        if removed:
            _hold(conn, current, removed, now, pause=True)
            write.observation_checkpoint(conn, ticket.source_id, now, reason=removed)
            return 0, removed
        if observation is None:
            if reason != "cancelled":
                _hold(conn, current, reason, now, pause=False)
            write.observation_checkpoint(conn, ticket.source_id, now, reason=reason)
            return 0, reason
        result = tapd_intake.observe_ticket(
            conn,
            _tapd(config),
            observation,
            publication_facts(conn, ticket),
            now=now,
        )
        if provider is None:
            reason = "provider_capability_unverified"
        elif result.reason != "observed":
            reason = result.reason
        write.observation_checkpoint(
            conn,
            ticket.source_id,
            now,
            completed=result.reason == "observed",
            reason=reason,
        )
        return len(result.request_ids), reason


def has_scan_work(db_path, config):
    """A disabled scan is needed only to revoke previously persisted scope."""
    if _tapd(config).get("enabled") is True:
        return True
    with closing(database.connect(db_path)) as conn:
        return bool(read.candidates(conn, (), configured=False, limit=1, outstanding_only=True))


def scan_sources(
    db_path,
    config,
    *,
    dry_run=False,
    client_factory=TapdClient,
    observation_provider=None,
    limits=ScanLimits(),
    stop=None,
):
    """Advance bounded discovery and refresh existing durable TAPD identities."""
    if dry_run:
        return ScanOutcome("dry_run")
    tapd = _tapd(config)
    enabled = tapd.get("enabled") is True
    projects = _projects(tapd) if enabled else {}
    started_at = time.monotonic()
    budget = ReadBudget(started_at + limits.seconds)
    discovery_budget = ReadBudget(started_at + limits.seconds / 2)
    attention = []
    with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
        transactions.begin_immediate(conn)
        removed = read.candidates(conn, tuple(projects), configured=False, limit=limits.tickets, outstanding_only=True)
        for row in removed:
            reason = _removed_scope(config, None, row)
            now = values.utc_now()
            _hold(conn, row, reason, now, pause=True)
            write.observation_checkpoint(conn, row["source_id"], now, reason=reason)
            attention.append(ScanAttention(row["source_id"], reason))
    if not enabled:
        return ScanOutcome("disabled", paused_sources=len(removed), attention=tuple(attention))

    with closing(database.connect(db_path)) as conn:
        probes = _ordered_probes(conn, _probes(tapd, projects))
    completed = 0
    candidates = set()
    try:
        client = _make_client(tapd, client_factory)
    except (TapdError, OSError, ValueError, KeyError):
        attention.append(ScanAttention("tapd", "provider_unavailable"))
        client = None
    for probe in probes[: limits.probes] if client is not None else ():
        try:
            _running(discovery_budget, stop)
        except TapdError as exc:
            attention.append(ScanAttention(probe.key, exc.code))
            break
        started = values.utc_now()
        with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            write.discovery_checkpoint(conn, probe.key, started)
        try:
            discovered = _candidate_tickets(
                client, tapd["host"], probe, budget=discovery_budget, limits=limits, stop=stop
            )
            _running(discovery_budget, stop)
        except (TapdError, OSError, ValueError, TypeError) as exc:
            reason = exc.code if isinstance(exc, TapdError) else "invalid_response"
            attention.append(ScanAttention(probe.key, reason))
            with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
                transactions.begin_immediate(conn)
                write.discovery_checkpoint(conn, probe.key, started, reason=reason)
            continue
        with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            for ticket in sorted(discovered, key=lambda ticket: ticket.key):
                _register(conn, ticket)
            completed += write.discovery_checkpoint(conn, probe.key, started, completed=True)
        candidates.update(discovered)

    with closing(database.connect(db_path)) as conn:
        targets = read.candidates(conn, tuple(projects), configured=True, limit=limits.tickets)
    checked = requests = 0
    paused = len(removed)
    for row in targets:
        key = row["source_id"]
        project = projects[(row["host"], row["project_key"])]
        with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            current = read.source(conn, key)
            if current is None:
                continue
            removed_reason = _removed_scope(config, project, current)
            reason = removed_reason or ("provider_unavailable" if client is None else None)
            if reason:
                now = values.utc_now()
                _hold(conn, current, reason, now, pause=bool(removed_reason))
                write.observation_checkpoint(conn, key, now, reason=reason)
        if reason:
            paused += int(removed_reason is not None)
            attention.append(ScanAttention(key, reason))
            checked += 1
            continue
        try:
            _running(budget, stop)
            request_count, reason = _refresh_candidate(
                db_path,
                config,
                row,
                project,
                client,
                observation_provider,
                budget=budget,
                limits=limits,
                stop=stop,
            )
        except TapdError as exc:
            attention.append(ScanAttention(key, exc.code))
            break
        except ValueError:
            request_count, reason = 0, "invalid_source_identity"
        checked += 1
        requests += request_count
        if reason:
            attention.append(ScanAttention(key, reason))
    if observation_provider is None:
        attention.append(ScanAttention("tapd", "provider_capability_unverified"))
    status = "attention" if attention else "observed"
    return ScanOutcome(status, completed, len(candidates), checked, paused, requests, tuple(attention))


def _claim_after_read(conn, ticket, owner, *, existing_claim, lease_seconds, now):
    if existing_claim is not None:
        if existing_claim.source_id != ticket.source_id or existing_claim.owner != owner:
            return None
        if source_sessions.renew(conn, existing_claim, now=now, lease_seconds=lease_seconds):
            return existing_claim
        if source_sessions.lock_claim(conn, existing_claim, now=now):
            return existing_claim
        return None
    return source_sessions.claim(conn, source_id=ticket.source_id, owner=owner, now=now, lease_seconds=lease_seconds)


def make_source_refresh(
    db_path,
    config,
    *,
    owner,
    claim=None,
    client_factory=TapdClient,
    observation_provider=None,
    github_discussion_runner=None,
    limits=ScanLimits(),
    stop=None,
    lease_seconds=120,
):
    """Create a fresh read callback for the source controller and dispatcher."""
    tapd = _tapd(config)
    projects = _projects(tapd) if tapd.get("enabled") is True else {}

    def refresh(ticket, *, task_id=None, attempt_id=None, read_stop=None):
        active_stop = stop if read_stop is None else read_stop
        project = projects.get((ticket.host, ticket.workspace_id))
        if project is None:
            raise ValueError("TAPD source is outside current configuration")
        with closing(database.connect(db_path)) as conn:
            row = read.source(conn, ticket.source_id)
        if row is None or _removed_scope(config, project, row):
            raise ValueError("TAPD source is outside current configuration")
        if (task_id is None) != (attempt_id is None):
            raise ValueError("TAPD refresh requires an exact task and attempt pair")
        try:
            client = _make_client(tapd, client_factory)
            budget = ReadBudget(time.monotonic() + limits.seconds)
            observation = _read_ticket(
                client,
                ticket,
                project,
                observation_provider,
                budget=budget,
                limits=limits,
                stop=active_stop,
            )
        except TapdError as exc:
            raise ValueError(exc.code) from None
        except (OSError, KeyError, TypeError):
            raise ValueError("TAPD observation unavailable") from None
        with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            row = read.source(conn, ticket.source_id)
            if row is None or _removed_scope(config, project, row):
                raise ValueError("TAPD source is outside current configuration")
            if task_id is not None:
                binding = admission.native_dispatch_binding(conn, task_id, attempt_id)
                if binding is None or binding[1]["source_id"] != ticket.source_id:
                    raise ValueError("TAPD attempt is outside the source")
            held = _claim_after_read(
                conn, ticket, owner, existing_claim=claim, lease_seconds=lease_seconds, now=values.utc_now()
            )
            if held is None:
                raise ValueError("TAPD source claim is unavailable")
            publication = publication_facts(conn, ticket)
            refreshed = admission.SourceDispatchObservation(held, observation, publication)
            if task_id is None or not source_members.round_github_request_ids(conn, binding[1]["round_id"]):
                return refreshed
            now = values.utc_now()
            processed = tapd_intake.observe_ticket(conn, tapd, observation, publication, now=now)
            round_ = source_sessions.lock_round(conn, held, binding[1]["round_id"], now=now)
            active = source_github_observation.active_scope(conn, round_) if round_ is not None else None
            if not processed.revision or active is None:
                return refreshed
            target = admission.worker_observation_target(task_id, attempt_id, binding, observation.observed_at)

        operations, _, failure = source_github_observation.read_operations(
            db_path,
            config,
            held,
            ticket,
            revision=processed.revision,
            request_ids=active.request_ids,
            runner=github_discussion_runner,
            stop=active_stop,
            active=active,
            target=target,
        )
        if failure:
            return refreshed
        with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            if (
                admission.native_dispatch_binding(conn, task_id, attempt_id) != binding
                or publication_facts(conn, ticket) != publication
            ):
                return refreshed
            result = tapd_intake.refresh_mixed_round(
                conn,
                config,
                ticket,
                held,
                active=active,
                revision=processed.revision,
                operations=operations,
                replacement_id=None,
                now=values.utc_now(),
                target=target,
            )
        return replace(refreshed, mixed=result.worker_proof)

    return refresh
