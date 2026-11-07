"""Offline, fail-closed DBOS-to-SQLite execution handoff.

This module never loads a DBOS SDK or performs Worker/GitHub effects. DBOS's
system database and worktrees must be retained as backup evidence by operators.
"""

from robert_agent.foundation.contracts.delivery import PublicationStatus
from robert_agent.foundation.contracts.execution import AttemptStatus, BindingStatus
from robert_agent.foundation.contracts.work import TERMINAL_REMEDIATION_STATUSES, TERMINAL_TASK_LIFECYCLES

from robert_agent.persistence.schema import execution_control as execution_schema

from robert_agent.persistence.queries import legacy_inventory

from robert_agent.persistence import database as db_database
from robert_agent.persistence import errors as db_errors
from robert_agent.persistence import transactions as db_transactions
from robert_agent.persistence.dialects import sqlite as db_sqlite

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import execution_migrations as db_read_execution_migrations
from robert_agent.persistence.queries import sqlite_master as db_read_sqlite_master
from robert_agent.persistence.stores import execution_migrations as db_write_execution_migrations
from robert_agent.persistence.stores import execution_pending_facts as db_write_execution_pending_facts
from robert_agent.persistence.stores import execution_subjects as db_write_execution_subjects
from robert_agent.persistence.stores import workflow_bindings as db_write_workflow_bindings

from contextlib import ExitStack, closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from robert_agent.persistence import execution_control as execution_store
from robert_agent.work import wakeup
from robert_agent.adapters.processes import identity as worker_identity
from robert_agent.delivery.lock import task_publication_lock

_TABLES = (
    "workflow_bindings",
    "workflow_outbox",
    "workflow_signal_receipts",
    "tasks",
    "attempts",
    "worker_results",
    "github_actions",
    "publication_intents",
    "github_events",
    "task_events",
    "workstreams",
    "pr_remediation_episodes",
    "wakeups",
    "leases",
    "daemon_runs",
    "agent_runs",
    "execution_subjects",
    "execution_pending_facts",
    "execution_fact_receipts",
    "notifications",
)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _connect(path, write=False):
    path = Path(path).expanduser().resolve(strict=True)
    conn = db_database.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=0.1)
    db_sqlite.configure(conn, foreign_keys=True)
    if not write:
        db_sqlite.configure(conn, query_only=True)
    return conn


def _rows(conn, table):
    return sorted((dict(row) for row in legacy_inventory.rows(conn, table)), key=_json)


def _inventory(conn, path):
    snapshot = {name: _rows(conn, name) for name in _TABLES}
    spool = []
    for file in sorted(path.parent.joinpath(path.name + ".results").glob("*.json")):
        spool.append({"name": file.name, "sha256": hashlib.sha256(file.read_bytes()).hexdigest()})
    fingerprint = _digest({"tables": snapshot, "spool": spool})
    blockers = []
    subjects = []
    mappings = []
    covered_subjects, covered_signals = execution_store.migration_coverage(conn)
    bindings = {
        b["workflow_id"]: b
        for b in snapshot["workflow_bindings"]
        if b["owner_engine"] == "dbos" and (b["subject_type"], b["subject_id"]) not in covered_subjects
    }
    for b in bindings.values():
        item = {
            k: b[k]
            for k in (
                "subject_type",
                "subject_id",
                "binding_status",
                "recovery_attempts",
                "definition_version",
            )
        }
        subjects.append(item)
        if b["definition_version"] != "v2" and b["binding_status"] != BindingStatus.TERMINAL:
            blockers.append({"subject_id": b["subject_id"], "reason": "unsupported_v1"})
        if any(
            row["subject_type"] == b["subject_type"] and row["subject_id"] == b["subject_id"]
            for row in snapshot["execution_subjects"]
        ):
            blockers.append({"subject_id": b["subject_id"], "reason": "execution_control_conflict"})
        metadata = json.loads(b["metadata_json"])
        if metadata.get("dispatch_inflight"):
            blockers.append({"subject_id": b["subject_id"], "reason": "dispatch_outcome_unknown"})
    for binding in bindings.values():
        if binding["subject_type"] == "task":
            domain = next(
                (t for t in snapshot["tasks"] if t["task_id"] == binding["subject_id"]),
                None,
            )
            terminal = domain and domain["lifecycle"] in TERMINAL_TASK_LIFECYCLES
        else:
            domain = next(
                (e for e in snapshot["pr_remediation_episodes"] if e["episode_id"] == binding["subject_id"]),
                None,
            )
            terminal = domain and domain["status"] in TERMINAL_REMEDIATION_STATUSES
        if not domain:
            blockers.append(
                {
                    "subject_id": binding["subject_id"],
                    "reason": "domain_subject_missing",
                }
            )
        elif binding["binding_status"] == BindingStatus.TERMINAL and not terminal:
            blockers.append(
                {
                    "subject_id": binding["subject_id"],
                    "reason": "terminal_binding_nonterminal_domain",
                }
            )
    for attempt in snapshot["attempts"]:
        dispatch = json.loads(attempt["metadata_json"] or "{}").get("dispatch", {})
        if dispatch and (dispatch.get("pid") or dispatch.get("process_identity")):
            identity = worker_identity.inspect_recorded_identity(dispatch)
            if identity != "dead":
                blockers.append(
                    {
                        "attempt_id": attempt["attempt_id"],
                        "reason": "worker_identity_" + identity,
                    }
                )
        if attempt["status"] in {AttemptStatus.RUNNING, AttemptStatus.STALE}:
            blockers.append({"attempt_id": attempt["attempt_id"], "reason": "worker_must_drain"})
    for intent in snapshot["publication_intents"]:
        if intent["status"] == PublicationStatus.EXECUTING:
            blockers.append(
                {
                    "action_id": intent["action_id"],
                    "reason": "publication_outcome_unknown",
                }
            )
    for run in snapshot["agent_runs"]:
        if run["status"] == "running":
            blockers.append({"run_id": run["run_id"], "reason": "agent_run_must_stop"})
    # An expired lease is not evidence that its old process has stopped.
    for lease in snapshot["leases"]:
        if lease["status"] == "active":
            blockers.append({"lease_id": lease["lease_id"], "reason": "active_execution_lease"})
    for daemon in snapshot["daemon_runs"]:
        if daemon["status"] == "running":
            blockers.append({"daemon_run_id": daemon["daemon_run_id"], "reason": "daemon_must_stop"})
    for file in sorted(path.parent.joinpath(path.name + ".results").glob("*.json")):
        try:
            if json.loads(file.read_text()).get("status") == "pending":
                blockers.append({"reason": "result_spool_must_drain", "file": file.name})
        except (ValueError, TypeError, AttributeError):
            blockers.append({"reason": "invalid_result_spool", "file": file.name})
    receipts = {r["signal_id"]: r for r in snapshot["workflow_signal_receipts"]}
    events = {e["event_fingerprint"]: e for e in snapshot["github_events"]}
    results = {r["result_id"]: r for r in snapshot["worker_results"]}
    tasks = {t["task_id"]: t for t in snapshot["tasks"]}
    streams = {w["workstream_id"]: w for w in snapshot["workstreams"]}
    episodes = {e["episode_id"]: e for e in snapshot["pr_remediation_episodes"]}
    for signal in snapshot["workflow_outbox"]:
        if signal["signal_id"] in covered_signals:
            continue
        receipt = receipts.get(signal["signal_id"])
        if receipt:
            if (receipt["workflow_id"], receipt["topic"]) != (
                signal["workflow_id"],
                signal["topic"],
            ):
                blockers.append({"signal_id": signal["signal_id"], "reason": "conflicting_receipt"})
            else:
                mappings.append(
                    {
                        "signal_id": signal["signal_id"],
                        "disposition": "already_consumed",
                    }
                )
            continue
        binding = bindings.get(signal["workflow_id"])
        if not binding:
            blockers.append({"signal_id": signal["signal_id"], "reason": "signal_binding_missing"})
            continue
        try:
            payload = json.loads(signal["payload_json"])
            fact = payload["fact_id"]
            valid = all(
                payload.get(k) == v
                for k, v in {
                    "signal_id": signal["signal_id"],
                    "topic": signal["topic"],
                    "subject_type": binding["subject_type"],
                    "subject_id": binding["subject_id"],
                }.items()
            )
        except (ValueError, KeyError, TypeError):
            valid = False
            fact = None
        if not valid:
            blockers.append({"signal_id": signal["signal_id"], "reason": "invalid_signal"})
            continue
        common = {
            "signal_id": signal["signal_id"],
            "subject_type": binding["subject_type"],
            "subject_id": binding["subject_id"],
            "topic": signal["topic"],
            "dedupe_key": signal["dedupe_key"],
        }
        if binding["subject_type"] == "task":
            task = tasks.get(binding["subject_id"])
        else:
            episode = episodes.get(binding["subject_id"])
            task = tasks.get(episode["last_task_id"]) if episode else None
        stream = streams.get(task["workstream_id"]) if task else None
        if fact in results and task and results[fact]["task_id"] == task["task_id"]:
            mappings.append(
                {
                    **common,
                    "disposition": "worker_result",
                    "fact_id": fact,
                    "task_id": task["task_id"],
                    "repo_id": stream["repo_id"],
                    "not_before_at": signal["not_before_at"],
                    "payload_json": _json(results[fact]),
                }
            )
        elif (
            fact in events
            and stream
            and json.loads(events[fact]["payload_json"]).get("workstream_id") == stream["workstream_id"]
        ):
            # Existing event ingestion remains responsible for trust, snapshot and
            # remediation budgets. A wakeup cannot authorize an action itself.
            mappings.append(
                {
                    **common,
                    "disposition": "github_event",
                    "fact_id": fact,
                    "task_id": task["task_id"],
                    "repo_id": stream["repo_id"],
                    "not_before_at": signal["not_before_at"],
                    "payload_json": events[fact]["payload_json"],
                }
            )
        else:
            blockers.append({"signal_id": signal["signal_id"], "reason": "unresolved_signal"})
    return {
        "format_version": 1,
        "database": str(path),
        "fingerprint": fingerprint,
        "ok": not blockers,
        "status": "ready" if not blockers else "blocked",
        "subjects": subjects,
        "signals": mappings,
        "blockers": blockers,
    }, snapshot


def preview(db_path):
    path = Path(db_path).expanduser().resolve(strict=True)
    with closing(_connect(path)) as conn:
        db_transactions.begin(conn, immediate=False)
        plan, _ = _inventory(conn, path)
        return plan


def apply(db_path, plan):
    path = Path(db_path).expanduser().resolve(strict=True)
    if not isinstance(plan, dict) or plan.get("database") != str(path) or plan.get("format_version") != 1:
        return {"ok": False, "status": "invalid_plan"}
    with closing(_connect(path, write=True)) as conn, conn, ExitStack() as locks:
        try:
            db_transactions.begin_exclusive(conn)
        except db_errors.OperationalError:
            return {"ok": False, "status": "database_busy"}
        if db_read_sqlite_master.object_exists(conn, "execution_migrations"):
            done = db_records.first(db_read_execution_migrations.find_for_apply(conn, plan.get("fingerprint")))
            if done:
                return {"ok": True, "status": "already_applied"}
        current, snapshot = _inventory(conn, path)
        if current["fingerprint"] != plan.get("fingerprint"):
            return {"ok": False, "status": "stale_plan"}
        if not current["ok"]:
            return current
        if not locks.enter_context(task_publication_lock(path, "dispatch-admission")):
            return {"ok": False, "status": "dispatch_in_progress"}
        for task in snapshot["tasks"]:
            if not locks.enter_context(task_publication_lock(path, task["task_id"])):
                return {"ok": False, "status": "publication_in_progress"}
        covered_subjects, _ = execution_store.migration_coverage(conn)
        if not current["subjects"] and not current["signals"]:
            return {
                "ok": True,
                "status": "already_migrated" if covered_subjects else "not_needed",
            }
        for notice in snapshot["notifications"]:
            if not locks.enter_context(task_publication_lock(path, "notify-" + notice["notification_id"])):
                return {"ok": False, "status": "notification_in_progress"}
        execution_schema.initialize(conn)
        execution_store.import_legacy_controls(conn)
        now = datetime.now(timezone.utc).isoformat()
        for binding in snapshot["workflow_bindings"]:
            if (
                binding["owner_engine"] != "dbos"
                or (binding["subject_type"], binding["subject_id"]) in covered_subjects
            ):
                continue
            db_write_execution_subjects.insert_for_apply(
                conn,
                *tuple(
                    (
                        binding[k]
                        for k in (
                            "subject_type",
                            "subject_id",
                            "workflow_kind",
                            "binding_status",
                            "recovery_attempts",
                            "created_at",
                            "updated_at",
                            "terminal_at",
                            "metadata_json",
                        )
                    )
                ),
            )
            execution_store.record_historical_facts(conn, binding, snapshot["workflow_outbox"])
            # Original binding is archived in the journal, then fenced from old
            # controllers. This does not change Task/Episode lifecycle.
            db_write_workflow_bindings.update_for_apply(conn, now, now, binding["subject_type"], binding["subject_id"])
        for mapping in current["signals"]:
            if mapping["disposition"] == "already_consumed":
                continue
            db_write_execution_pending_facts.insert_for_apply(
                conn,
                *tuple(
                    (
                        mapping[k]
                        for k in (
                            "signal_id",
                            "subject_type",
                            "subject_id",
                            "topic",
                            "dedupe_key",
                            "fact_id",
                            "disposition",
                            "repo_id",
                            "task_id",
                            "payload_json",
                            "not_before_at",
                        )
                    )
                ),
            )
            wakeup.request_wakeup(
                conn,
                repo_id=mapping["repo_id"],
                reason="manual_operator_request",
                dedupe_key="dbos-migration:" + mapping["signal_id"],
                task_id=mapping["task_id"],
                result_id=mapping["fact_id"] if mapping["disposition"] == "worker_result" else None,
                metadata={
                    "kind": "dbos_handoff",
                    "signal_id": mapping["signal_id"],
                    "fact_id": mapping["fact_id"],
                    "disposition": mapping["disposition"],
                },
                not_before_at=mapping["not_before_at"],
                now=now,
            )
        db_write_execution_migrations.insert_for_apply(
            conn,
            "dbos-to-sqlite:" + current["fingerprint"][:24],
            current["fingerprint"],
            now,
            _json(snapshot),
            _json(current["signals"]),
        )
        return {
            "ok": True,
            "status": "migrated",
            "subjects": len(current["subjects"]),
            "signals": len(current["signals"]),
            "migration_id": "dbos-to-sqlite:" + current["fingerprint"][:24],
        }


def run(db_path, *, plan_file=None, apply_changes=False):
    """CLI adapter; a plan file is explicit operator output, never DB state."""
    if apply_changes:
        if not plan_file:
            raise ValueError("--apply requires --plan-file from a fresh --dry-run")
        plan = json.loads(Path(plan_file).expanduser().read_text())
        return apply(db_path, plan)
    plan = preview(db_path)
    if plan_file:
        path = Path(plan_file).expanduser()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(plan, output, indent=2, ensure_ascii=False)
            output.write("\n")
    return plan
