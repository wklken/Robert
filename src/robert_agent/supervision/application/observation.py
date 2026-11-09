"""Collect bounded supervision evidence without advancing Robert's workflows."""

from robert_agent.foundation.contracts.delivery import AuditStatus, OutputType, PublicationStatus, PublishStatus
from robert_agent.foundation.contracts.execution import AttemptStatus
from robert_agent.foundation.contracts.work import TaskLifecycle, WorkstreamLifecycle

from robert_agent.persistence.queries import observation as observation_queries

from robert_agent.persistence.queries import repos as db_read_repos

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_actions as db_read_github_actions
from robert_agent.persistence.queries import retries as db_read_retries
from robert_agent.persistence.queries import task_events as db_read_task_events

from robert_agent.persistence import database as db_database
from robert_agent.persistence import errors as db_errors
from robert_agent.persistence import transactions as db_transactions

from robert_agent.persistence.queries import artifacts as db_read_artifacts
from robert_agent.persistence.queries import attempts as db_read_attempts
from robert_agent.persistence.queries import daemon_events as db_read_daemon_events
from robert_agent.persistence.queries import leases as db_read_leases
from robert_agent.persistence.queries import run_repo_steps as db_read_run_repo_steps
from robert_agent.persistence.queries import sqlite_master as db_read_sqlite_master
from robert_agent.persistence.queries import supervision_checkpoints as db_read_supervision_checkpoints
from robert_agent.persistence.queries import supervision_incidents as db_read_supervision_incidents
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import work_item_events as db_read_work_item_events

from collections import Counter
from contextlib import closing
from datetime import timedelta
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time

from robert_agent.delivery.application import audit as audit_result
from robert_agent.execution import capacity as execution_capacity
from robert_agent.work import operator_queries as github_operator
from robert_agent.execution import spool as result_spool
from robert_agent.workflows import routes as route
from robert_agent.supervision import receipts
from robert_agent.supervision import rules as supervision_rules
from robert_agent.adapters.processes import identity as worker_identity
from robert_agent.execution import command as worker_heartbeat


DB_SECONDS = 4
OBSERVATION_SECONDS = 9
MAX_SPOOL_BYTES = 256 * 1024
MAX_ACTIONS_PER_RESULT = 100
LANE_COMPLETIONS = {
    "local": ("local_drain_completed",),
    "scan": ("scan_completed", "live_poll_completed"),
    "publish": ("publish_completed",),
}


class ObservationUnavailable(ValueError):
    """No trustworthy snapshot is available to classify."""

    def __init__(self, reason_code):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _object(value):
    decoded = json.loads(value or "{}") if isinstance(value, (str, bytes)) else None
    return decoded if isinstance(decoded, dict) else {}


def database_error_reason(exc):
    name = getattr(exc, "sqlite_errorname", "")
    message = str(exc)
    if not name:
        # Python 3.10 exposes SQLite's message but no symbolic error name.
        name = {
            "database is locked": "SQLITE_BUSY",
            "database table is locked": "SQLITE_LOCKED",
            "database schema is locked": "SQLITE_LOCKED",
            "database disk image is malformed": "SQLITE_CORRUPT",
            "file is not a database": "SQLITE_NOTADB",
            "interrupted": "SQLITE_INTERRUPT",
        }.get(message, "")
        if message.startswith(("database table is locked: ", "database schema is locked: ")):
            name = "SQLITE_LOCKED"
        elif message.startswith(("no such table: ", "no such column: ")):
            name = "SQLITE_ERROR"
    if name.startswith(("SQLITE_BUSY", "SQLITE_LOCKED")):
        return "database_busy"
    if name.startswith(("SQLITE_CORRUPT", "SQLITE_NOTADB")):
        return "database_corrupt"
    if name == "SQLITE_INTERRUPT":
        return "database_observation_deadline"
    if name == "SQLITE_ERROR" and ("no such table" in message or "no such column" in message):
        return "database_schema_incompatible"
    return "database_unavailable"


def _controller_facts(conn, db_path, now, config):
    lease = db_records.first(db_read_leases.find_for_controller_facts(conn, "database:" + str(db_path)))
    configured = config.get("daemon", {}).get("enabled", True)
    status, run_id = "unverified", lease["owner_id"] if lease else None
    if not configured:
        status = "disabled"
    elif lease:
        expires = supervision_rules.timestamp(lease["expires_at"])
        if lease["run_status"] in {"failed", "stopped"}:
            status = lease["run_status"]
        elif expires:
            status = "fresh" if lease["lease_status"] == "active" and expires > now else "expired"
    controller = {"scope_id": "daemon", "status": status, "daemon_run_id": run_id}
    if lease:
        controller["lease_id"] = lease["lease_id"]
        controller["lease_expires_at"] = lease["expires_at"]
        heartbeat = supervision_rules.timestamp(lease["heartbeat_at"])
        acquired = supervision_rules.timestamp(lease["acquired_at"])
        controller["has_progress"] = bool(acquired and heartbeat and acquired < heartbeat <= now)
        controller["heartbeat_at"] = lease["heartbeat_at"]
    facts = [controller]
    for lane, event_types in LANE_COMPLETIONS.items():
        lane_fact = {
            "scope_id": "daemon:" + lane,
            "daemon_run_id": run_id,
            "status": status if status in {"disabled", "stopped"} else "unverified",
        }
        if run_id and configured and status not in {"failed", "stopped"}:
            # A same-run child completion cannot clear a daemon attention latch.
            attention = db_records.first(db_read_daemon_events.find_for_controller_facts(conn, run_id, lane))
            if attention:
                lane_fact.update(status="attention", event_id=attention["daemon_event_id"], cause=attention["cause"])
            elif status == "fresh":
                event = db_records.first(db_read_daemon_events.controller_facts_filtered(conn, event_types, run_id))
                if event and event["status"] == "ok":
                    lane_fact.update(
                        status="completed", event_id=event["daemon_event_id"], completed_at=event["created_at"]
                    )
        facts.append(lane_fact)
    return facts


def _task_page(conn, now, cursor, limit, config):
    has_incidents = bool(db_read_sqlite_master.table_exists(conn, "supervision_incidents"))
    repos = [repo["full_name"] for repo in config.get("repos", ())]
    return observation_queries.task_page(
        conn, cursor, limit, (now - timedelta(hours=1)).isoformat(), repos, has_incidents
    )


def _attempt_facts(conn, task_id):
    return db_records.first(db_read_attempts.find_for_attempt_facts(conn, task_id))


def _publication_facts(conn, facts, now):
    actions = db_read_github_actions.find_for_publication_facts(
        conn, now, facts["result_id"], MAX_ACTIONS_PER_RESULT + 1
    )
    pending = [action for action in actions if action["publish_status"] == PublishStatus.NOT_PUBLISHED]
    if pending:
        action = next((row for row in pending if row["eligible"]), pending[0])
        relevant = (action["retry_at"], facts.get("process_stopped_at"), facts.get("audited_at"), action["created_at"])
        eligible_at = max(filter(None, map(supervision_rules.timestamp, relevant)), default=None)
        facts.update(
            pending_action_id=action["action_id"],
            intent_status=action["intent_status"],
            intent_started_at=action["intent_started_at"],
            publish_reason=action["publish_reason"],
            publication_terminal_failure=action["intent_status"] == PublicationStatus.ATTENTION
            or (action["retryable"] == 0 and not action["reconcile_required"]),
            publication_eligible=bool(action["eligible"]),
            publication_retry_at=action["retry_at"],
            action_created_at=action["created_at"],
            action_eligible_at=eligible_at.isoformat() if eligible_at else None,
        )
    if facts["lifecycle"] == TaskLifecycle.COMPLETED or (
        actions and all(a["publish_status"] == PublishStatus.PUBLISHED for a in actions)
    ):
        _delivery_facts(facts, actions)
        if actions and facts["delivery_status"] == "confirmed":
            facts["publication_receipt"] = facts["delivery_positive"]


def _delivery_facts(facts, actions):
    def outcome(status, reason, positive=None):
        facts.update(delivery_status=status, delivery_reason=reason, delivery_positive=positive)

    facts["action_count"] = len(actions)
    if len(actions) > MAX_ACTIONS_PER_RESULT or not facts["expected_output"] or facts["planned_actions"] is None:
        return outcome("unknown", "delivery_plan_unverified")
    plan = json.loads(facts["planned_actions"])
    if not isinstance(plan, list) or not all(
        isinstance(action, dict) and isinstance(action.get("type"), str) for action in plan
    ):
        return outcome("unknown", "delivery_plan_unverified")
    facts["planned_action_count"] = len(plan)
    expected = facts["expected_output"]
    actual_output = facts["output_type"]
    if (actual_output != expected and actual_output != "waiting_for_user") or audit_result._audit_expected_actions(
        actual_output, [a["type"] for a in plan]
    ):
        return outcome("inconsistent", "delivery_output_contract_mismatch")
    actual = []
    publications = []
    for action in actions:
        payload = _object(action["metadata_json"])
        publication = payload.pop("publish", {})
        publications.append(publication if isinstance(publication, dict) else {})
        if payload.get("type") != action["action_type"] or action["task_id"] != facts["task_id"]:
            return outcome("inconsistent", "delivery_action_binding_mismatch")
        actual.append(payload)
    if Counter(map(supervision_rules._digest, plan)) != Counter(map(supervision_rules._digest, actual)):
        return outcome("inconsistent", "planned_actions_do_not_match")
    if facts["audit_status"] != AuditStatus.ACCEPTED or any(a["audit_status"] != AuditStatus.ACCEPTED for a in actions):
        return outcome("inconsistent", "completed_delivery_not_audited")
    if any(a["publish_status"] == PublishStatus.SKIPPED for a in actions):
        # Existing business finalization permits superseded review/vote and remediation.
        return outcome("retired", "delivery_action_retired")
    if any(a["publish_status"] != PublishStatus.PUBLISHED for a in actions):
        return outcome("inconsistent", "completed_delivery_not_published")
    scope = _object(facts["audited_scope"])
    confirmations = []
    for action, publish in zip(actions, publications):
        receipt = publish.get("receipt")
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema_version") != 1
            or not scope
            or not action["intent_receipt"]
        ):
            return outcome("unknown", "delivery_receipt_unverified")
        if not receipts.matches(facts, action, scope, receipt):
            return outcome("inconsistent", "delivery_receipt_binding_mismatch")
        confirmations.append(supervision_rules.timestamp(receipt["confirmed_at"]))
    occurred_at = max(confirmations) if confirmations else supervision_rules.timestamp(facts.get("audited_at"))
    if occurred_at is None:
        return outcome("unknown", "delivery_occurrence_time_unverified")
    return outcome(
        "confirmed",
        "delivery_verified" if actions else "local_delivery_verified",
        {
            "result_id": facts["result_id"],
            "action_ids": [a["action_id"] for a in actions],
            "occurred_at": occurred_at.isoformat(),
        },
    )


def _checkpoints(conn, scope_type, scope_id):
    rows = db_read_supervision_checkpoints.find_for_checkpoints(conn, scope_type, scope_id)
    return {
        row["rule_id"]: {"evidence": _object(row["evidence_json"]), "eligible_since": row["eligible_since"]}
        for row in rows
    }


def _execution_recovery(conn, facts, now):
    """Find a completed retry of the same request through explicit inheritance."""
    lineage = facts.get("lineage_key")
    if (
        not lineage
        or facts["lifecycle"] not in {TaskLifecycle.COMPLETED, TaskLifecycle.FAILED}
        or facts.get("canceled")
    ):
        return None
    if not db_records.first(db_read_supervision_incidents.find_for_execution_recovery(conn, facts["task_id"], lineage)):
        return None
    # Bounded traversal through durable parents. A separate request may share
    # a repository or source without being a continuation of this failure.
    candidates = db_read_retries.find_for_execution_recovery(conn, facts["task_id"], facts["repo_id"], lineage)
    failed_at = (
        supervision_rules.timestamp(facts.get("finished_at")) if facts["lifecycle"] == TaskLifecycle.FAILED else None
    )
    for candidate in candidates:
        completed = _completed_execution(conn, candidate, now)
        occurred_at = supervision_rules.timestamp((completed or {}).get("occurred_at"))
        if occurred_at and (failed_at is None or occurred_at > failed_at):
            return completed
    return None


def _completed_execution(conn, candidate, now):
    attempt = _attempt_facts(conn, candidate["task_id"])
    if not attempt or attempt["output_type"] in {OutputType.CLASSIFICATION_RESULT, OutputType.WAITING_FOR_USER}:
        return None
    proof = _completed_attempt_delivery(conn, candidate, attempt, now)
    return (
        {**proof, "successor_task_id": candidate["task_id"], "successor_parent_task_id": candidate["parent_task_id"]}
        if proof
        else None
    )


def _completed_attempt_delivery(conn, candidate, attempt, now):
    if not attempt or attempt["attempt_status"] != AttemptStatus.COMPLETED or not attempt["result_id"]:
        return None
    if type(_object(attempt["dispatch"]).get("pid")) is int and not attempt["process_stopped_at"]:
        return None
    evidence = {**dict(candidate), **dict(attempt)}
    _publication_facts(conn, evidence, now)
    proof = evidence.get("delivery_positive")
    return {**proof, "attempt_id": attempt["attempt_id"]} if proof else None


def _task_facts(conn, row, controller_wait, now, capacity, consumer_completed):
    facts = dict(row)
    if not facts["repo_enabled"]:
        facts["runnable"] = False
    facts.update(controller_wait)
    facts["local_completed_at"] = consumer_completed(facts["repo_id"], "github_route")
    facts["checkpoints"] = _checkpoints(conn, "task", facts["task_id"])
    if supervision_rules._future(supervision_rules.timestamp(now), facts["repository_busy_until"]):
        facts["local_wait"] = "repository_busy"
    handoff = _object(facts.pop("pending_handoff"))
    attempt = _attempt_facts(conn, facts["task_id"])
    if attempt:
        facts.update(dict(attempt))
        facts["dispatch"] = _object(facts["dispatch"])
        facts["process_recorded"] = type(facts["dispatch"].get("pid")) is int
        facts["audit_eligible_at"] = facts["audit_retry_at"] or facts["process_stopped_at"] or facts["finished_at"]
        if facts["result_id"]:
            _publication_facts(conn, facts, now)
        if facts["attempt_status"] == AttemptStatus.PREPARED:
            _dispatch_facts(conn, facts, capacity)
        elif facts["attempt_status"] in {AttemptStatus.RUNNING, AttemptStatus.STALE} and not facts["result_id"]:
            _milestones(conn, facts)
    facts["generation"] = {
        "kind": "task",
        **{
            key: facts.get(key)
            for key in ("lifecycle", "task_updated_at", "attempt_id", "attempt_status", "result_id", "audit_status")
        },
    }
    if handoff:
        child = db_records.first(
            db_read_tasks.find_for_task_facts(conn, facts["task_id"], handoff.get("child_task_id"))
        )
        facts.update(
            handoff_status=handoff.get("status"),
            handoff_retry_at=handoff.get("next_retry_at"),
            successor_task_id=child["task_id"] if child else None,
            successor_occurred_at=child["created_at"] if child else None,
            handoff_due_at=handoff.get("next_retry_at") or facts["task_updated_at"],
            handoff_current=(
                facts["active_task_id"] == facts["task_id"]
                and facts["stream_lifecycle"] == WorkstreamLifecycle.ACTIVE
                and handoff.get("retryable") is not False
            ),
        )
    facts["execution_recovery_evidence"] = _execution_recovery(conn, facts, now)
    return facts


def _dispatch_facts(conn, facts, capacity):
    facts["has_prompt"] = bool(
        db_records.first(db_read_artifacts.find_for_dispatch_facts(conn, facts["task_id"], facts["attempt_id"]))
    )
    route_info = route.ROUTES.get(facts["route_id"], {})
    facts["has_worktree"] = bool(facts["worktree_path"] or not route_info.get("needs_worktree", True))
    facts["dispatch_block"] = (
        "worker_unavailable" if facts["dispatch_wait"] else capacity(facts["repo_id"], facts["full_name"])
    )


def _milestones(conn, facts):
    rows = db_read_artifacts.find_for_milestones(conn, facts["task_id"], facts["attempt_id"])
    if rows and len(rows) <= 64:
        facts["milestones"] = sorted({(a["artifact_type"], a["sha256"]) for a in rows})
        facts["milestone_observed_at"] = rows[0]["created_at"]


def _receipt_page(conn, now, position, limit, config):
    repos = [repo["full_name"] for repo in config.get("repos", ())]
    return observation_queries.receipt_page(conn, *position, (now - timedelta(hours=1)).isoformat(), limit, repos)


def _receipt_facts(conn, row, consumer_completed, wait):
    facts = {
        **_object(row["metadata_json"]),
        "scope_type": row["scope_type"],
        "scope_id": row["scope_id"],
        "lineage_key": row["lineage_key"] or None,
        "disposition_at": row["disposition_at"],
    }
    scope, key = facts["scope_type"], facts["scope_id"]
    facts["generation"] = {"kind": "admission", "metadata_digest": supervision_rules._digest(row["metadata_json"])}
    if row["source_closed"] or not row["repo_enabled"]:
        wait = "request_no_longer_active"
    elif scope == "work_item_event" and db_records.first(db_read_work_item_events.find_for_receipt_facts(conn, key)):
        wait = "request_no_longer_active"
    facts["wait_reason"] = wait
    facts["checkpoints"] = _checkpoints(conn, scope, key)
    target, target_id = facts.get("target_type"), facts.get("target_id")
    disposition = facts.get("disposition")
    if target in {"review_round", "vote_round"}:
        facts["target_exists"] = observation_queries.round_exists(conn, target, target_id, facts.get("repo_id"))
    elif target == "task":
        owner = db_records.first(db_read_tasks.find_for_receipt_facts(conn, target_id, facts.get("repo_id")))
        facts["target_exists"] = bool(owner)
        if owner and disposition == "linked_pending" and scope == "github_event":
            successor = db_records.first(db_read_task_events.find_for_receipt_facts(conn, key, target_id, target_id))
            facts["successor_task_id"] = successor["task_id"] if successor else None
            facts["successor_occurred_at"] = successor["occurred_at"] if successor else None
            facts["pending_eligible"] = (
                owner["lifecycle"] == TaskLifecycle.COMPLETED
                and owner["active_task_id"] == target_id
                and owner["stream_lifecycle"] == WorkstreamLifecycle.ACTIVE
            )
        elif owner and scope == "work_item_event":
            facts["operator_handoff"] = True
            if owner["lifecycle"] == TaskLifecycle.DETECTED:
                pending = github_operator.pending_tasks(conn, task_id=target_id)
                if pending and not github_operator.blocked(conn, pending[0]):
                    observed, response = github_operator.source_read(conn, pending[0])
                    facts["operator_handoff_eligible"] = bool(observed and response)
            else:
                attempt = db_records.first(db_read_attempts.find_started_at_by_task_id(conn, target_id))
                if attempt:
                    facts.update(successor_task_id=target_id, successor_occurred_at=attempt["started_at"])
    if disposition in {None, "deferred", "linked_pending"} or facts.get("operator_handoff_eligible"):
        facts["consumer_completed_at"] = consumer_completed(facts.get("repo_id"), facts.get("consumer"))
    return facts


def _page_positions(cursor):
    if not cursor.startswith("{"):
        return {"task": cursor, "receipt": ["", ""], "next": "task", "done": []}
    value = json.loads(cursor)
    if (
        not isinstance(value, dict)
        or set(value) != {"task", "receipt", "next", "done"}
        or not isinstance(value["task"], str)
        or not isinstance(value["receipt"], list)
        or len(value["receipt"]) != 2
        or not all(isinstance(key, str) for key in value["receipt"])
        or value["next"] not in ("task", "receipt")
        or not isinstance(value["done"], list)
        or any(kind not in ("task", "receipt") for kind in value["done"])
    ):
        raise ValueError("invalid supervision cursor")
    return value


def _mix_pages(pages, first, limit):
    positions = {"task": 0, "receipt": 0}
    kind = first
    for _ in range(limit):
        other = "receipt" if kind == "task" else "task"
        if positions[kind] >= len(pages[kind]):
            kind, other = other, kind
        if positions[kind] >= len(pages[kind]):
            return
        yield kind, pages[kind][positions[kind]]
        positions[kind] += 1
        kind = other


def _command_facts(db_path, facts, pgid, deadline):
    # The worker writes records to its standard artifact directory even when
    # command output itself is redirected. Never read stdout or stderr here.
    task, attempt = facts["task_id"], facts["attempt_id"]
    if worker_heartbeat.safe_filename_part(task) != task:
        return
    prefix = worker_heartbeat.safe_filename_part(attempt) + "."
    failures = Counter()
    limited = False
    try:
        with os.scandir(worker_heartbeat.default_output_dir(db_path, task)) as entries:
            for number, entry in enumerate(entries):
                if number >= 64 or time.monotonic() >= deadline:
                    limited = True
                    break
                if not entry.name.startswith(prefix) or not entry.name.endswith(".command.json"):
                    continue
                try:
                    fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            continue
                        data = stream.read(65537)
                    record = json.loads(data) if len(data) <= 65536 else {}
                except (OSError, ValueError, RecursionError):
                    limited = True
                    continue
                if not isinstance(record, dict) or (record.get("task_id"), record.get("attempt_id")) != (task, attempt):
                    continue
                if record.get("status") in {"running", "starting"}:
                    wrapper_pid = record.get("wrapper_pid")
                    try:
                        if type(wrapper_pid) is int and wrapper_pid > 1 and os.getpgid(wrapper_pid) == pgid:
                            facts["command_in_progress"] = True
                    except OSError:
                        pass
                elif record.get("status") == "failed":
                    signature = supervision_rules._digest(
                        [record.get("phase"), record.get("argv"), record.get("returncode")]
                    )
                    failures[signature] += 1
    except OSError:
        limited = True
    facts["command_failures"] = max(failures.values(), default=0)
    facts["command_observation"] = "limited" if limited else "journals_sampled"


def _observe_process(dispatch, ps_runner, live_groups):
    pid = dispatch.get("pid")
    if type(pid) is not int or pid <= 1:
        return "unknown"
    if dispatch.get("process_exited"):
        identity = "dead"
    else:
        identity = worker_identity.inspect_recorded_identity(dispatch, ps_runner=ps_runner)
    if identity != "dead":
        return identity
    groups = live_groups()
    if groups is None:
        return "unknown"
    return "group_alive" if pid in groups else "dead"


def _spool_facts(db_path, task_id, attempt_id):
    key = hashlib.sha256(attempt_id.encode()).hexdigest()
    path = result_spool.directory(db_path) / (key + ".json")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return {"spool_status": "unverified"}
            raw = stream.read(MAX_SPOOL_BYTES + 1)
        if len(raw) > MAX_SPOOL_BYTES:
            return {"spool_status": "unverified"}
        record = json.loads(raw)
        result_spool._validate_record(record)
        if record["payload"].get("task_id") == task_id and record["payload"].get("attempt_id") == attempt_id:
            return {
                "spool_status": "saved",
                "spool_saved_at": record.get("saved_at"),
                "spool_fingerprint": record["fingerprint"],
            }
    except FileNotFoundError:
        return {"spool_status": "missing"}
    except (OSError, ValueError, RecursionError):
        pass
    return {"spool_status": "unverified"}


def _dead_worker_snapshot_current(db_path, facts, dispatch):
    # Spool replay may have committed a result and removed its file since the
    # first snapshot. Do not report a result-less exit from that mixed view.
    with closing(db_database.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=0.1)) as conn:
        fresh = _attempt_facts(conn, facts["task_id"])
    return bool(
        fresh
        and not fresh["result_id"]
        and not fresh["process_stopped_at"]
        and all(fresh[key] == facts.get(key) for key in ("attempt_id", "attempt_status", "heartbeat_at"))
        and _object(fresh["dispatch"]) == dispatch
    )


def is_current(conn, observation):
    """Check receipt/attempt versions before persisting a decisive observation."""
    generation = observation.get("generation")
    if not generation:
        return True
    scope = observation["scope_id"]
    if generation.get("kind") == "admission":
        row = db_records.first(
            db_read_supervision_checkpoints.find_for_is_current(conn, observation["scope_type"], scope)
        )
        return bool(row and supervision_rules._digest(row[0]) == generation["metadata_digest"])
    row = db_records.first(db_read_tasks.find_lifecycle_and_updated_at_by_task_id(conn, scope))
    if not row or tuple(row) != (generation["lifecycle"], generation["task_updated_at"]):
        return False
    attempt = db_records.first(db_read_attempts.find_for_is_current(conn, scope))
    current = tuple(attempt) if attempt else (None, None, None, None)
    if current != tuple(generation[key] for key in ("attempt_id", "attempt_status", "result_id", "audit_status")):
        return False
    rule = observation["rule_id"]
    if rule == "ready_not_dispatched" and observation["classification"] == "confirmed":
        return bool(
            db_records.first(
                db_read_leases.find_for_is_current(conn, observation["evidence"].get("local_daemon_run_id"))
            )
        )
    proof = observation.get("positive_evidence")
    if not proof:
        return True
    if rule == "ready_not_dispatched" and observation["reason_code"] == "prepared_attempt_delivery_verified":
        candidate = db_records.first(db_read_tasks.find_for_is_current_2(conn, scope))
        return bool(
            candidate
            and _completed_attempt_delivery(conn, candidate, _attempt_facts(conn, scope), proof["occurred_at"]) == proof
        )
    if rule != "execution_failed":
        return True
    # Recheck the exact completed retry and the receipt version before closing
    # the original incident. A changed result waits for the next observation.
    cursor = db_read_tasks.find_for_is_current(conn, proof["successor_task_id"], observation["lineage_key"])
    candidate = db_records.first(cursor)
    if candidate is None:
        return False
    facts = dict(candidate)
    return _completed_execution(conn, facts, proof["occurred_at"]) == proof


def collect(db_path, *, now, limit=100, config=None, cursor=""):
    """Return a page of tasks/admissions and the constant-size controller projection.

    Candidates share one read-only snapshot. Independent keysets alternate so
    a backlog of tasks cannot starve old admissions or vice versa.
    """
    now = supervision_rules.timestamp(now)
    if now is None or type(limit) is not int or not 1 <= limit <= 500 or not isinstance(cursor, str):
        raise ValueError("invalid observation time, limit or cursor")
    positions = _page_positions(cursor)
    config = config or {}
    settings = config.get("supervision", {})
    poll = settings.get("poll_seconds", 60)
    db_path = Path(db_path).expanduser().resolve()
    started = time.monotonic()
    deadline = started + OBSERVATION_SECONDS
    db_deadline = started + DB_SECONDS
    try:
        with closing(db_database.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=0.1)) as conn:
            conn.set_progress_handler(lambda: int(time.monotonic() >= db_deadline), 1000)
            db_transactions.begin(conn, immediate=False)
            controllers = _controller_facts(conn, db_path, now, config)
            controller_wait = {}
            if controllers[0]["status"] != "fresh" and config:
                controller_wait.update(local_wait="controller_unavailable", publish_wait="controller_unavailable")
            for facts in controllers[1:]:
                lane = facts["scope_id"].split(":", 1)[1]
                if lane == "local":
                    controller_wait.update(
                        dispatch_consumer_run_id=facts.get("daemon_run_id"),
                        dispatch_completed_at=facts.get("completed_at"),
                    )
                if facts.get("completed_at") and lane == "publish":
                    controller_wait["publish_completed_at"] = facts["completed_at"]
                if facts["status"] == "attention":
                    controller_wait[lane + "_wait"] = "controller_lane_latched"
                    if facts.get("cause") == "blocked_result_contract_cutover":
                        controller_wait.update(
                            local_wait="result_contract_cutover", publish_wait="result_contract_cutover"
                        )
            pages = {
                "task": []
                if "task" in positions["done"]
                else _task_page(conn, now, positions["task"], limit + 1, config),
                "receipt": []
                if "receipt" in positions["done"]
                else _receipt_page(conn, now, positions["receipt"], limit + 1, config),
            }
            repos = {repo["full_name"]: repo for repo in config.get("repos", ())}
            max_concurrency = config.get("max_concurrency")

            @lru_cache(maxsize=1)
            def full():
                if max_concurrency is None or not repos:
                    return False
                repo_ids = [row[0] for row in db_read_repos.full_filtered(conn, repos)]
                return execution_capacity.capacity_full_for_repos(conn, repo_ids, max_concurrency)

            @lru_cache(maxsize=limit)
            def capacity(repo_id, full_name):
                if full():
                    return "global_capacity"
                repo_limit = repos.get(full_name, {}).get("max_concurrency") or max_concurrency
                if repo_limit is not None and execution_capacity.capacity_full(conn, repo_id, repo_limit):
                    return "repository_capacity"
                return None

            @lru_cache(maxsize=limit)
            def consumer_completed(repo_id, consumer):
                if consumer not in (None, "github_route"):
                    return None
                row = db_records.first(db_read_run_repo_steps.find_for_consumer_completed(conn, repo_id))
                return row[0]

            candidates = [
                (
                    kind,
                    _task_facts(conn, row, controller_wait, now.isoformat(), capacity, consumer_completed)
                    if kind == "task"
                    else _receipt_facts(conn, row, consumer_completed, controller_wait.get("local_wait")),
                )
                for kind, row in _mix_pages(pages, positions["next"], limit)
            ]
    except db_errors.Error as exc:
        raise ObservationUnavailable(database_error_reason(exc)) from exc
    except (ValueError, RecursionError) as exc:
        raise ObservationUnavailable("database_evidence_invalid") from exc

    def ps_runner(command, **kwargs):
        available = deadline - time.monotonic()
        if available <= 0:
            raise subprocess.TimeoutExpired(command, 0)
        kwargs["timeout"] = min(kwargs.get("timeout", 2), available)
        return subprocess.run(command, **kwargs)

    @lru_cache(maxsize=1)
    def live_groups():
        try:
            result = ps_runner(["ps", "-axo", "pgid=,stat="], capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode:
            return None
        return {
            int(fields[0])
            for line in result.stdout.splitlines()
            if len(fields := line.split()) == 2 and fields[0].isdigit() and fields[1][0] not in "ZXx"
        }

    observations = [supervision_rules.evaluate_controller(facts, now=now, poll_seconds=poll) for facts in controllers]
    visited = {"task": 0, "receipt": 0}
    stall = settings.get("stall_seconds", 300)
    for kind, facts in candidates:
        if any(visited.values()) and time.monotonic() >= deadline:
            break
        visited[kind] += 1
        positions["next"] = "receipt" if kind == "task" else "task"
        if kind == "receipt":
            positions[kind] = [facts["scope_type"], facts["scope_id"]]
            observations.extend(
                supervision_rules.evaluate_admission(facts, now=now, poll_seconds=poll, stall_seconds=stall)
            )
            continue
        positions[kind] = facts["task_id"]
        dispatch = facts.pop("dispatch", {})
        if dispatch and not facts.get("process_stopped_at"):
            facts["process_status"] = _observe_process(dispatch, ps_runner, live_groups)
        if (
            facts.get("attempt_id")
            and not facts.get("result_id")
            and facts.get("attempt_status") in {AttemptStatus.RUNNING, AttemptStatus.STALE, AttemptStatus.PREPARED}
        ):
            facts.update(_spool_facts(db_path, facts["task_id"], facts["attempt_id"]))
        if facts.get("process_status") == "dead" and facts.get("spool_status") == "missing":
            try:
                current = time.monotonic() < deadline and _dead_worker_snapshot_current(db_path, facts, dispatch)
            except (db_errors.Error, ValueError):
                current = False
            if not current:
                facts["spool_status"] = "unverified"
        if facts.get("process_status") == "alive_same" and not facts.get("result_id"):
            _command_facts(db_path, facts, dispatch.get("pid"), deadline)
        observations.extend(supervision_rules.evaluate_task(facts, now=now, poll_seconds=poll, stall_seconds=stall))
    positions["done"] = [kind for kind in pages if visited[kind] == len(pages[kind])]
    complete = len(positions["done"]) == len(pages)
    return {
        "observations": observations,
        "coverage_complete": complete,
        "cursor": "" if complete else json.dumps(positions, separators=(",", ":")),
    }
