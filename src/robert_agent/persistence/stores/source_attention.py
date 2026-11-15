"""Persist coordination attention and its outbox in the caller transaction."""

import json

from sqlalchemy import insert, update

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries.source_attention import (
    CORRELATION,
    NOTICE_TYPE,
    PUBLICATION_NOTICE_TYPE,
    QUESTION_NOTICE_TYPE,
    RECOVERY_NOTICE_TYPE,
)
from robert_agent.persistence.stores import notifications


def _open_notification(conn, notification_id, notification_type, now):
    if not conn.in_transaction:
        raise ValueError("Source attention requires the caller's writer")
    if (
        notifications.insert_notification(
            conn, notification_id, None, notification_type, now, json.dumps({CORRELATION: notification_id})
        )
        != 1
    ):
        raise ValueError("Unable to persist source attention notification")


def open_coordination(conn, *, notification_id, source_id, subject, cause, version, now):
    _open_notification(conn, notification_id, NOTICE_TYPE, now)
    statements.write(
        conn,
        insert(tables["source_attentions"]).values(
            notification_id=notification_id,
            source_id=source_id,
            round_id=subject.round_id,
            kind="coordination",
            cause=cause,
            version=version,
            repo_id=subject.repo_id,
            repo_basis=subject.repo_basis,
            task_id=subject.task_id,
            attempt_id=subject.attempt_id,
            model_run_id=subject.model_run_id,
            created_at=now,
        ),
    )


def open_question(conn, *, notification_id, root, action, subject, body, version, now, receipts=None):
    """Bind the outbox to the question and receipt the caller completed."""
    root_evidence = root["evidence_json"]
    if receipts is not None:
        root_evidence = json.dumps({"version": 1, "root_evidence": root_evidence, "receipts": receipts}, sort_keys=True)
    _open_notification(conn, notification_id, QUESTION_NOTICE_TYPE, now)
    statements.write(
        conn,
        insert(tables["source_attentions"]).values(
            notification_id=notification_id,
            source_id=root["source_id"],
            round_id=subject.round_id,
            kind="ordinary_question",
            cause="source_question_delivered",
            version=version,
            repo_id=subject.repo_id,
            repo_basis=subject.repo_basis,
            task_id=subject.task_id,
            attempt_id=subject.attempt_id,
            model_run_id=subject.model_run_id,
            created_at=now,
        ),
    )
    statements.write(
        conn,
        insert(tables["source_question_attentions"]).values(
            notification_id=notification_id,
            source_result_id=root["source_result_id"],
            publication_id=action["publication_id"],
            body=body,
            result_evidence_json=root_evidence,
            prepared_evidence_json=action["prepared_evidence_json"],
            remote_evidence_json=action["remote_evidence_json"],
            intent_fingerprint=action["intent_fingerprint"],
        ),
    )


def resolve_coordination(conn, *, source_id, notification_id, now):
    attention, notice = (tables[name] for name in ("source_attentions", "notifications"))
    changed = statements.write(
        conn,
        update(attention)
        .where(
            attention.c.notification_id == notification_id,
            attention.c.source_id == source_id,
            attention.c.kind == "coordination",
            attention.c.resolved_at.is_(None),
        )
        .values(resolved_at=now),
    )
    if changed != 1:
        raise ValueError("Source attention changed before resolution")
    if (
        statements.write(
            conn, update(notice).where(notice.c.notification_id == notification_id).values(status="resolved")
        )
        != 1
    ):
        raise ValueError("Source attention notification is missing")


def open_recovery(conn, *, notification_id, pause, subject, cause, version, now):
    _open_notification(conn, notification_id, RECOVERY_NOTICE_TYPE, now)
    statements.write(
        conn,
        insert(tables["source_attentions"]).values(
            notification_id=notification_id,
            source_id=pause["source_id"],
            round_id=subject.round_id,
            kind="delivery_recovery",
            cause=cause,
            version=version,
            repo_id=subject.repo_id,
            repo_basis=subject.repo_basis,
            task_id=subject.task_id,
            attempt_id=subject.attempt_id,
            created_at=now,
        ),
    )
    statements.write(
        conn,
        insert(tables["source_recovery_attentions"]).values(
            notification_id=notification_id,
            source_result_id=pause["source_result_id"],
            generation=pause["generation"],
            pause_proof_json=pause["proof_json"],
        ),
    )


def resolve_recovery(conn, *, notification_id, source_id, now):
    attention, notice = tables["source_attentions"], tables["notifications"]
    changed = statements.write(
        conn,
        update(attention)
        .where(
            attention.c.notification_id == notification_id,
            attention.c.source_id == source_id,
            attention.c.kind == "delivery_recovery",
            attention.c.resolved_at.is_(None),
        )
        .values(resolved_at=now),
    )
    if changed != 1:
        raise ValueError("Delivery recovery attention changed before resolution")
    if (
        statements.write(
            conn,
            update(notice)
            .where(notice.c.notification_id == notification_id, notice.c.notification_type == RECOVERY_NOTICE_TYPE)
            .values(status="resolved"),
        )
        != 1
    ):
        raise ValueError("Delivery recovery notification is missing")


def publication_binding(subject):
    return {
        name: subject[name]
        for name in ("source_id", "source_result_id", "action_id", "publication_id", "request_fingerprint", "provider")
    }


def open_publication(conn, *, notification_id, subject, cause, version, now):
    _open_notification(conn, notification_id, PUBLICATION_NOTICE_TYPE, now)
    statements.write(
        conn,
        insert(tables["source_publication_attentions"]).values(
            notification_id=notification_id,
            cause=cause,
            version=version,
            created_at=now,
            **publication_binding(subject),
        ),
    )


def resolve_publication(conn, *, notification_id, subject, now):
    attention, notice = (tables[name] for name in ("source_publication_attentions", "notifications"))
    if (
        statements.write(
            conn,
            update(attention)
            .where(
                attention.c.notification_id == notification_id,
                attention.c.resolved_at.is_(None),
                *(attention.c[name] == value for name, value in publication_binding(subject).items()),
            )
            .values(resolved_at=now),
        )
        != 1
    ):
        raise ValueError("Publication attention changed before resolution")
    if (
        statements.write(
            conn,
            update(notice)
            .where(notice.c.notification_id == notification_id, notice.c.notification_type == PUBLICATION_NOTICE_TYPE)
            .values(status="resolved"),
        )
        != 1
    ):
        raise ValueError("Publication attention notification is missing")
