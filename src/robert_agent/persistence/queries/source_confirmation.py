"""Read versioned controller questions, answer receipts and exact event bindings."""

from sqlalchemy import func, select

from robert_agent.foundation.contracts.delivery import SourceResultKind
from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables


def current(conn, source_id):
    table = tables["source_confirmation_questions"]
    return records.first(
        statements.read(conn, select(table).where(table.c.source_id == source_id, table.c.superseded_at.is_(None)))
    )


def question(conn, source_id, question_id):
    table = tables["source_confirmation_questions"]
    return records.first(
        statements.read(conn, select(table).where(table.c.source_id == source_id, table.c.question_id == question_id))
    )


def for_result(conn, source_id, source_result_id):
    table = tables["source_confirmation_questions"]
    return records.first(
        statements.read(
            conn, select(table).where(table.c.source_id == source_id, table.c.source_result_id == source_result_id)
        )
    )


def next_version(conn, source_id):
    table = tables["source_confirmation_questions"]
    return statements.read(
        conn, select(func.coalesce(func.max(table.c.version), 0) + 1).where(table.c.source_id == source_id)
    )[0][0]


def answer(conn, question_id):
    table = tables["source_confirmation_answers"]
    return records.first(statements.read(conn, select(table).where(table.c.question_id == question_id)))


def answered_event(conn, event_id):
    table = tables["source_confirmation_answers"]
    return records.first(statements.read(conn, select(table).where(table.c.event_id == event_id)))


def resume_transition(conn, source_id, transition_id):
    table = tables["source_resume_transitions"]
    return records.first(
        statements.read(
            conn, select(table).where(table.c.source_id == source_id, table.c.transition_id == transition_id)
        )
    )


def latest_resume_transition(conn, source_id, binding_request_id):
    transition, question = (tables[name] for name in ("source_resume_transitions", "source_confirmation_questions"))
    return records.first(
        statements.read(
            conn,
            select(transition.c.transition_id)
            .select_from(transition.join(question, question.c.question_id == transition.c.question_id))
            .where(transition.c.source_id == source_id, transition.c.binding_request_id == binding_request_id)
            .order_by(question.c.version.desc())
            .limit(1),
        )
    )


def applied_question(conn, question_id):
    table = tables["source_resume_transitions"]
    return bool(statements.read(conn, select(table.c.transition_id).where(table.c.question_id == question_id)))


def prior_accepted_roots(conn, source_id):
    """Read result roots to distinguish fully settled delivery from recovery."""
    table = tables["source_results"]
    return statements.read(
        conn,
        select(table)
        .where(table.c.source_id == source_id, table.c.kind != SourceResultKind.CONTROL_NOTICE.value)
        .order_by(table.c.accepted_at, table.c.source_result_id),
    )
