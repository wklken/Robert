"""Materialized canonical review and per-trigger completion facts."""

from sqlalchemy import select

from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables


def by_key(conn, key):
    table = tables["review_executions"]
    return records.first(statements.read(conn, select(table).where(table.c.canonical_key == key)))


def trigger(conn, repo, number, fingerprint):
    table = tables["review_triggers"]
    return records.first(
        statements.read(
            conn,
            select(table).where(
                table.c.repo == repo, table.c.pr_number == number, table.c.trigger_fingerprint == fingerprint
            ),
        )
    )


def triggers(conn, round_id):
    table = tables["review_triggers"]
    return statements.read(
        conn,
        select(table).where(table.c.round_id == round_id).order_by(table.c.created_at, table.c.trigger_fingerprint),
    )


def published_actions(conn, task_id):
    actions, results = tables["github_actions"], tables["worker_results"]
    return statements.read(
        conn,
        select(actions, results.c.attempt_id, results.c.metadata_json.label("result_metadata_json"))
        .select_from(actions.join(results, results.c.result_id == actions.c.result_id))
        .where(
            actions.c.task_id == task_id,
            actions.c.action_type == "submit_pr_review",
            actions.c.audit_status == "accepted",
            actions.c.publish_status == "published",
        ),
    )


def pending_trigger_rounds(conn, repo):
    triggers, rounds = tables["review_triggers"], tables["review_sessions"]
    return [
        row[0]
        for row in statements.read(
            conn,
            select(triggers.c.round_id)
            .distinct()
            .select_from(triggers.join(rounds, rounds.c.round_id == triggers.c.round_id))
            .where(
                triggers.c.repo == repo,
                triggers.c.status != "completed",
                rounds.c.task_id.is_not(None),
                rounds.c.status == "published",
            ),
        )
    ]
