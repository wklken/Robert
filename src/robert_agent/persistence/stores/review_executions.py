"""Caller-owned, atomic canonical review and trigger writes."""

from sqlalchemy import update, or_
from sqlalchemy.dialects.sqlite import insert

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables


def register(conn, key, round_id, identity_json, now):
    table = tables["review_executions"]
    return statements.write(
        conn,
        insert(table)
        .values(canonical_key=key, round_id=round_id, identity_json=identity_json, created_at=now)
        .on_conflict_do_nothing(index_elements=[table.c.canonical_key]),
    )


def attach(conn, *, repo, number, fingerprint, round_id, workstream_id, request_json, authorization_json, now):
    table = tables["review_triggers"]
    return statements.write(
        conn,
        insert(table)
        .values(
            repo=repo,
            pr_number=number,
            trigger_fingerprint=fingerprint,
            round_id=round_id,
            workstream_id=workstream_id,
            request_json=request_json,
            authorization_json=authorization_json,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=[table.c.repo, table.c.pr_number, table.c.trigger_fingerprint]),
    )


def bind(conn, round_id, task_id, now):
    table = tables["review_triggers"]
    return statements.write(
        conn,
        update(table)
        .where(
            table.c.round_id == round_id,
            table.c.status != "completed",
            or_(table.c.task_id.is_(None), table.c.task_id != task_id),
        )
        .values(task_id=task_id, status="bound", updated_at=now),
    )


def complete(conn, round_id, task_id, result_id, action_id, completion_json, now):
    table = tables["review_triggers"]
    return statements.write(
        conn,
        update(table)
        .where(table.c.round_id == round_id, table.c.task_id == task_id, table.c.status != "completed")
        .values(
            status="completed",
            result_id=result_id,
            action_id=action_id,
            completion_json=completion_json,
            updated_at=now,
        ),
    )
