"""Caller-transaction SQLite execution controls and publication intents.

Historical workflow tables remain available to the offline upgrade tool only.
Execution ownership is represented by the existing repository leases and attempt
identity; these records preserve recovery limits and stopped-state evidence.
"""

from robert_agent.foundation.contracts.delivery import PublicationStatus
from robert_agent.foundation.contracts.execution import BindingStatus
from robert_agent.foundation.contracts.work import TERMINAL_REMEDIATION_STATUSES, TERMINAL_TASK_LIFECYCLES, TaskKind

from sqlalchemy import select, literal_column, tuple_, update, func
from robert_agent.persistence.catalog import tables as model_tables
from robert_agent.persistence import statements

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import (
    execution_migrations as db_read_execution_migrations,
)
from robert_agent.persistence.queries import (
    execution_pending_facts as db_read_execution_pending_facts,
)
from robert_agent.persistence.queries import (
    execution_subjects as db_read_execution_subjects,
)
from robert_agent.persistence.queries import github_actions as db_read_github_actions
from robert_agent.persistence.queries import (
    pr_remediation_episodes as db_read_pr_remediation_episodes,
)
from robert_agent.persistence.queries import (
    publication_intents as db_read_publication_intents,
)
from robert_agent.persistence.queries import sqlite_master as db_read_sqlite_master
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import (
    workflow_bindings as db_read_workflow_bindings,
)
from robert_agent.persistence.queries import workflow_outbox as db_read_workflow_outbox
from robert_agent.persistence.queries import (
    workflow_signal_receipts as db_read_workflow_signal_receipts,
)
from robert_agent.persistence.stores import (
    execution_fact_receipts as db_write_execution_fact_receipts,
)
from robert_agent.persistence.stores import (
    execution_pending_facts as db_write_execution_pending_facts,
)
from robert_agent.persistence.stores import (
    execution_subjects as db_write_execution_subjects,
)
from robert_agent.persistence.stores import (
    publication_intents as db_write_publication_intents,
)
from robert_agent.persistence.stores import wakeups as db_write_wakeups

from robert_agent.persistence.schema import execution_control as execution_schema

import hashlib
import json


class MigrationRequiredError(RuntimeError):
    """Expected upgrade refusal, safe to report at user command boundaries."""


def get_binding(conn, subject_type, subject_id):
    return _row(db_read_execution_subjects.find_for_get_binding(conn, subject_type, subject_id))


def read_binding(conn, subject_type, subject_id):
    """Read old controls without writing; a neutral row wins per subject."""
    tables = {r[0] for r in db_read_sqlite_master.find_for_read_binding(conn)}
    for table in ("execution_subjects", "workflow_bindings"):
        if table in tables:
            row = _row(
                statements.read(
                    conn,
                    select(literal_column("*"))
                    .select_from(model_tables[table])
                    .where(
                        model_tables[table].c.subject_type == subject_type,
                        model_tables[table].c.subject_id == subject_id,
                    )
                    .limit(1),
                )
            )
            if row:
                return row
    return None


def read_controls(conn, subjects=None):
    controls = {}
    if subjects is not None:
        subjects = sorted(set(subjects))
        if not subjects:
            return controls
    tables = {r[0] for r in db_read_sqlite_master.find_for_read_binding(conn)}
    for table in ("workflow_bindings", "execution_subjects"):
        if table in tables:
            # Keep bounded parameter counts for older supported SQLite builds.
            batches = [None] if subjects is None else [subjects[i : i + 200] for i in range(0, len(subjects), 200)]
            for batch in batches:
                relation = model_tables[table]
                query = select(literal_column("*")).select_from(relation)
                if batch is not None:
                    query = query.where(tuple_(relation.c.subject_type, relation.c.subject_id).in_(batch))
                cursor = statements.read(conn, query)
                for values in cursor:
                    row = dict(values)
                    controls[(row["subject_type"], row["subject_id"])] = row
    return controls


def ensure_subject(conn, subject_type, subject_id, workflow_kind, now):
    _validate_subject(conn, subject_type, subject_id, workflow_kind)
    db_write_execution_subjects.insert_for_ensure_subject(conn, subject_type, subject_id, workflow_kind, now, now)
    row = get_binding(conn, subject_type, subject_id)
    if row["workflow_kind"] != workflow_kind:
        raise ValueError("execution subject kind conflicts with its recorded kind")
    return row


def import_legacy_controls(conn):
    execution_schema.initialize(conn)
    db_write_execution_subjects.insert_for_import_legacy_controls(conn)


def migration_coverage(conn):
    tables = {r[0] for r in db_read_sqlite_master.find_for_read_binding(conn)}
    covered_subjects = set()
    covered_signals = set()
    if "execution_migrations" in tables:
        for (
            snapshot,
            mappings,
        ) in db_read_execution_migrations.find_for_migration_coverage(conn):
            original = json.loads(snapshot)
            covered_subjects.update(
                (b["subject_type"], b["subject_id"])
                for b in original["workflow_bindings"]
                if b["owner_engine"] == "dbos"
            )
            mapped = json.loads(mappings)
            for mapping in mapped:
                if mapping["disposition"] == "already_consumed":
                    receipt = db_records.first(
                        db_read_workflow_signal_receipts.find_for_migration_coverage(conn, mapping["signal_id"])
                    )
                    if not receipt:
                        raise MigrationRequiredError("Migration journal references a missing historical receipt")
                    continue
                row = _row(db_read_execution_pending_facts.find_record_by_source_signal_id(conn, mapping["signal_id"]))
                if not row or any(
                    row[key] != mapping[key]
                    for key in (
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
                ):
                    raise MigrationRequiredError("Migration journal conflicts with queued execution facts")
            covered_signals.update(m["signal_id"] for m in mapped)
    return covered_subjects, covered_signals


def require_migrated(conn):
    if not db_read_sqlite_master.object_exists(conn, "workflow_bindings"):
        return
    covered_subjects, covered_signals = migration_coverage(conn)
    for row in db_read_workflow_bindings.find_for_require_migrated(conn):
        key = (row[0], row[1])
        if key in covered_subjects:
            if row[2] != "terminal" or not get_binding(conn, *key):
                raise MigrationRequiredError("DBOS migration journal conflicts with execution controls")
        elif row[2] != "terminal":
            raise MigrationRequiredError(
                "DBOS execution requires offline migration: run robert migrate dbos --db PATH --dry-run"
            )
        elif row[0] == "task":
            task = db_records.first(db_read_tasks.find_lifecycle_by_task_id(conn, row[1]))
            if task and task[0] not in TERMINAL_TASK_LIFECYCLES:
                raise MigrationRequiredError("Terminal DBOS binding has a nonterminal task; offline migration required")
        else:
            episode = db_records.first(db_read_pr_remediation_episodes.find_status_by_episode_id(conn, row[1]))
            if episode and episode[0] not in TERMINAL_REMEDIATION_STATUSES:
                raise MigrationRequiredError(
                    "Terminal DBOS binding has a nonterminal episode; offline migration required"
                )
    for signal, workflow, topic in db_read_workflow_outbox.find_for_require_migrated(conn):
        receipt = db_records.first(
            db_read_workflow_signal_receipts.find_workflow_id_and_topic_by_signal_id(conn, signal)
        )
        if receipt and tuple(receipt) != (workflow, topic):
            raise MigrationRequiredError("Historical signal receipt conflicts with its source")
        if not receipt and signal not in covered_signals:
            raise MigrationRequiredError("Unconsumed DBOS signal requires offline migration")


def pending_facts(conn, repo_id, now):
    return [
        dict(row)
        for cursor in [db_read_execution_pending_facts.find_for_pending_facts(conn, repo_id, now)]
        for row in cursor
    ]


def consume_fact(conn, source_signal_id, now, reason="domain_effect_recorded"):
    # Call only in the transaction that records the corresponding domain effect.
    db_write_execution_pending_facts.update_for_consume_fact(conn, now, _json({"reason": reason}), source_signal_id)
    db_write_wakeups.update_for_consume_fact(conn, now, "dbos-migration:" + source_signal_id)


def record_historical_facts(conn, binding, signals=()):
    metadata = json.loads(binding.get("metadata_json") or "{}")
    latest = metadata.get("latest_fact")
    if isinstance(latest, dict) and latest.get("fact_id"):
        db_write_execution_fact_receipts.record_historical_facts(
            conn,
            binding["subject_type"],
            binding["subject_id"],
            latest["fact_id"],
            _json(latest),
        )
    for signal in signals:
        if signal["workflow_id"] == binding.get("workflow_id") and signal["topic"] == "subject-events":
            payload = json.loads(signal["payload_json"])
            if payload.get("fact_id"):
                db_write_execution_fact_receipts.record_historical_facts_2(
                    conn,
                    binding["subject_type"],
                    binding["subject_id"],
                    payload["fact_id"],
                )


_PUBLICATION_TRANSITIONS = {
    PublicationStatus.PREPARED.value: {PublicationStatus.EXECUTING.value},
    PublicationStatus.EXECUTING.value: {
        PublicationStatus.PUBLISHED.value,
        PublicationStatus.CONFIRMED_ABSENT.value,
        PublicationStatus.FAILED.value,
        PublicationStatus.ATTENTION.value,
    },
    PublicationStatus.CONFIRMED_ABSENT.value: {PublicationStatus.EXECUTING.value},
    PublicationStatus.FAILED.value: {PublicationStatus.EXECUTING.value},
    PublicationStatus.PUBLISHED.value: set(),
    PublicationStatus.ATTENTION.value: set(),
}


def _row(cursor):
    value = db_records.first(cursor)
    if value is None:
        return None
    return dict(value)


def _json(value):
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


def _id(prefix, *values):
    digest = hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _validate_subject(conn, subject_type, subject_id, workflow_kind):
    legal_pairs = {
        ("task", TaskKind.HUMAN_REQUEST.value),
        ("remediation_episode", "pr_remediation"),
    }
    if (subject_type, workflow_kind) not in legal_pairs:
        raise ValueError("illegal workflow subject and kind pairing")
    if subject_type == "task":
        row = db_records.first(db_read_tasks.find_task_kind_and_lifecycle_by_task_id(conn, subject_id))
        if not row:
            raise ValueError("workflow task subject does not exist")
        if row[0] != TaskKind.HUMAN_REQUEST.value or row[1] in TERMINAL_TASK_LIFECYCLES:
            raise ValueError("workflow task subject is not eligible")
        return
    row = db_records.first(db_read_pr_remediation_episodes.find_status_by_episode_id(conn, subject_id))
    if not row:
        raise ValueError("workflow remediation episode subject does not exist")
    if row[0] in TERMINAL_REMEDIATION_STATUSES:
        raise ValueError("workflow remediation episode subject is not eligible")


def close_binding(conn, subject_type, subject_id, now):
    binding = get_binding(conn, subject_type, subject_id)
    if not binding:
        raise ValueError("workflow subject is not bound")
    if binding["binding_status"] == BindingStatus.TERMINAL:
        return binding
    db_write_execution_subjects.update_for_close_binding(conn, now, now, subject_type, subject_id)
    return get_binding(conn, subject_type, subject_id)


def mark_binding_attention(conn, subject_type, subject_id, now):
    binding = get_binding(conn, subject_type, subject_id)
    if not binding:
        raise ValueError("workflow subject is not bound")
    if binding["binding_status"] == BindingStatus.ATTENTION:
        return binding
    if binding["binding_status"] == BindingStatus.TERMINAL:
        raise ValueError("terminal workflow binding cannot require attention")
    db_write_execution_subjects.mark_binding_attention(conn, now, subject_type, subject_id)
    return get_binding(conn, subject_type, subject_id)


def prepare_publication_intent(conn, action_id, idempotency_key, request_fingerprint, now):
    if not db_records.first(db_read_github_actions.find_for_prepare_publication_intent(conn, action_id)):
        raise ValueError("publication action does not exist")
    existing = _row(db_read_publication_intents.find_record_by_action_id(conn, action_id))
    if existing:
        if existing["idempotency_key"] == idempotency_key and existing["request_fingerprint"] == request_fingerprint:
            return existing
        raise ValueError("publication action already has a conflicting intent")
    existing_key = _row(db_read_publication_intents.find_record_by_idempotency_key(conn, idempotency_key))
    if existing_key:
        raise ValueError("publication idempotency key already belongs to another action")
    publication_id = _id("publication", action_id)
    db_write_publication_intents.insert_for_prepare_publication_intent(
        conn, publication_id, action_id, idempotency_key, request_fingerprint, now, now
    )
    return _row(db_read_publication_intents.find_record_by_publication_id(conn, publication_id))


def transition_publication_intent(conn, publication_id, status, now, remote_evidence=None, error=None):
    intent = _row(db_read_publication_intents.find_record_by_publication_id(conn, publication_id))
    if not intent:
        raise ValueError("publication intent does not exist")
    if intent["status"] == status:
        return intent
    if status not in _PUBLICATION_TRANSITIONS.get(intent["status"], set()):
        raise ValueError(f"illegal publication intent transition: {intent['status']} -> {status}")
    changes = {"status": status, "updated_at": now}
    table = model_tables["publication_intents"]
    if status == PublicationStatus.EXECUTING.value:
        changes.update(
            started_at=func.coalesce(table.c.started_at, now),
            finished_at=None,
            error_json=None,
        )
    if status in {
        PublicationStatus.PUBLISHED.value,
        PublicationStatus.CONFIRMED_ABSENT.value,
        PublicationStatus.FAILED.value,
        PublicationStatus.ATTENTION.value,
    }:
        changes["finished_at"] = now
    if remote_evidence is not None:
        changes["remote_evidence_json"] = _json(remote_evidence)
    if error is not None:
        changes["error_json"] = _json(error)
    statements.write(
        conn,
        update(table).where(table.c.publication_id == publication_id).values(changes),
    )
    return _row(db_read_publication_intents.find_record_by_publication_id(conn, publication_id))
