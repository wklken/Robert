"""Caller-transaction SQLite execution controls and publication intents.

Execution ownership is represented by the existing repository leases and attempt
identity; these records preserve recovery limits and stopped-state evidence.
"""

from robert_agent.foundation.contracts.delivery import PublicationStatus
from robert_agent.foundation.contracts.execution import BindingStatus
from robert_agent.foundation.contracts.work import TERMINAL_REMEDIATION_STATUSES, TERMINAL_TASK_LIFECYCLES, TaskKind

import hashlib
import json

from sqlalchemy import func, select, tuple_, update

from robert_agent.persistence import records as db_records, statements
from robert_agent.persistence.catalog import tables as model_tables
from robert_agent.persistence.queries import (
    execution_subjects as db_read_execution_subjects,
    github_actions as db_read_github_actions,
    pr_remediation_episodes as db_read_pr_remediation_episodes,
    publication_intents as db_read_publication_intents,
    tasks as db_read_tasks,
)
from robert_agent.persistence.stores import (
    execution_subjects as db_write_execution_subjects,
    publication_intents as db_write_publication_intents,
)


def get_binding(conn, subject_type, subject_id):
    return _row(db_read_execution_subjects.find_for_get_binding(conn, subject_type, subject_id))


def read_controls(conn, subjects=None):
    controls = {}
    relation = model_tables["execution_subjects"]
    query = select(relation)
    if subjects is None:
        batches = (None,)
    else:
        subjects = sorted(set(subjects))
        # Keep bounded parameter counts for supported SQLite builds.
        batches = (subjects[i : i + 200] for i in range(0, len(subjects), 200))
    for batch in batches:
        statement = query
        if batch is not None:
            statement = query.where(tuple_(relation.c.subject_type, relation.c.subject_id).in_(batch))
        for values in statements.read(conn, statement):
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
