"""Write scoped confirmation facts inside the caller transaction."""

import json

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.sqlite import insert

from robert_agent.foundation.contracts.delivery import PublicationStatus, PublishStatus
from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def insert_question(conn, *, question_id, source_id, version, scope, scope_digest, source_result_id, action_id, now):
    table = tables["source_confirmation_questions"]
    return statements.write(
        conn,
        insert(table).values(
            question_id=question_id,
            source_id=source_id,
            version=version,
            scope_json=_json(scope),
            scope_digest=scope_digest,
            source_result_id=source_result_id,
            action_id=action_id,
            created_at=now,
        ),
    )


def supersede(conn, question, now):
    table, action, intent = (
        tables[name]
        for name in (
            "source_confirmation_questions",
            "github_actions",
            "publication_intents",
        )
    )
    statements.write(
        conn,
        update(table)
        .where(
            table.c.question_id == question["question_id"],
            table.c.superseded_at.is_(None),
        )
        .values(superseded_at=now),
    )
    # A saved start remains eligible only for reconciliation; retain its intent.
    # PREPARED alone is safe to retire, but the guard still owns its disposition.
    blocking = (
        select(intent.c.publication_id)
        .where(
            intent.c.action_id == action.c.action_id,
            or_(
                intent.c.status != PublicationStatus.PREPARED.value,
                intent.c.started_at.is_not(None),
                intent.c.finished_at.is_not(None),
            ),
        )
        .correlate(action)
        .exists()
    )
    prepared = select(intent.c.publication_id).where(
        intent.c.action_id == question["action_id"],
        intent.c.status == PublicationStatus.PREPARED.value,
        intent.c.started_at.is_(None),
        intent.c.finished_at.is_(None),
    )
    retired = statements.write(
        conn,
        update(action)
        .where(
            action.c.action_id == question["action_id"],
            action.c.source_result_id == question["source_result_id"],
            action.c.publish_status == PublishStatus.NOT_PUBLISHED.value,
            ~blocking,
        )
        .values(publish_status=PublishStatus.SKIPPED.value),
    )
    if retired:
        statements.write(
            conn,
            update(intent)
            .where(intent.c.publication_id.in_(prepared))
            .values(
                status=PublicationStatus.CONFIRMED_ABSENT.value,
                finished_at=now,
                updated_at=now,
            ),
        )


def accept(conn, *, question_id, event_id, publication_id, comment_id, proof, now):
    table = tables["source_confirmation_answers"]
    return statements.write(
        conn,
        insert(table)
        .values(
            question_id=question_id,
            event_id=event_id,
            publication_id=publication_id,
            comment_id=comment_id,
            proof_json=_json(proof),
            accepted_at=now,
        )
        .on_conflict_do_nothing(),
    )


def revoke(conn, event_id, now):
    table = tables["source_confirmation_answers"]
    return statements.write(
        conn,
        update(table)
        .where(
            table.c.event_id == event_id,
            table.c.revoked_at.is_(None),
        )
        .values(revoked_at=now),
    )


def claim(conn, question_id, *, consumer_id, now):
    table = tables["source_confirmation_answers"]
    return bool(
        statements.write(
            conn,
            update(table)
            .where(
                table.c.question_id == question_id,
                table.c.revoked_at.is_(None),
                table.c.consumer_id.is_(None),
            )
            .values(consumer_id=consumer_id, claimed_at=now),
        )
    )


def record_resume(conn, *, source_id, transition_id, receipt, binding, repository, request_ids, now):
    """Record the applied transition and close its offer without changing the answer or effect."""
    if not conn.in_transaction:
        raise ValueError("Resume transition requires a caller-owned writer")
    transition, question = (tables[name] for name in ("source_resume_transitions", "source_confirmation_questions"))
    changed = statements.write(
        conn,
        insert(transition).values(
            transition_id=transition_id,
            source_id=source_id,
            question_id=receipt["question_id"],
            binding_request_id=binding["request_id"],
            paused_binding_json=_json(binding),
            receipt_json=_json(receipt),
            repository_json=_json(repository),
            request_ids_json=_json(request_ids),
            applied_at=now,
        ),
    )
    if (
        changed != 1
        or statements.write(
            conn,
            update(question)
            .where(
                question.c.question_id == receipt["question_id"],
                question.c.source_id == source_id,
                question.c.superseded_at.is_(None),
            )
            .values(superseded_at=now),
        )
        != 1
    ):
        raise ValueError("Resume transition lost its exact current offer")
