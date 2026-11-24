"""Exact result and publication evidence for an idle board analysis delivery."""

from sqlalchemy import select, tuple_

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.predicates import native_slot_owned


def analysis_result(conn, task, workstream_ids):
    results, attempts, tasks = tables["worker_results"], tables["attempts"], tables["tasks"]
    latest_result, latest_attempt = results.alias("latest_result"), attempts.alias("latest_attempt")
    links, wakeups = tables["task_events"], tables["wakeups"]
    newer_task = (
        select(1)
        .where(
            tasks.c.workstream_id.in_(workstream_ids),
            tuple_(tasks.c.created_at, tasks.c.task_id) > tuple_(task["created_at"], task["task_id"]),
        )
        .exists()
    )
    pending_input = (
        select(1)
        .select_from(links.join(tasks, tasks.c.task_id == links.c.task_id))
        .where(
            tasks.c.workstream_id.in_(workstream_ids),
            links.c.relationship.in_(("pending", "context")),
        )
        .exists()
    )
    pending_request = (
        select(1)
        .select_from(wakeups.join(tasks, tasks.c.task_id == wakeups.c.task_id))
        .where(
            tasks.c.workstream_id.in_(workstream_ids),
            wakeups.c.status == "pending",
        )
        .exists()
    )
    native_owned = select(1).where(tasks.c.workstream_id.in_(workstream_ids), native_slot_owned(tasks)).exists()
    query = (
        select(
            results.c.result_id,
            results.c.attempt_id,
            results.c.created_at,
            results.c.metadata_json,
            attempts.c.metadata_json.label("attempt_metadata_json"),
        )
        .select_from(results.join(attempts, attempts.c.attempt_id == results.c.attempt_id))
        .where(
            results.c.task_id == task["task_id"],
            attempts.c.task_id == task["task_id"],
            attempts.c.status == "completed",
            results.c.output_type == "comment_analysis",
            results.c.result_id
            == select(latest_result.c.result_id)
            .where(latest_result.c.task_id == task["task_id"])
            .order_by(latest_result.c.created_at.desc(), latest_result.c.result_id.desc())
            .limit(1)
            .scalar_subquery(),
            attempts.c.attempt_id
            == select(latest_attempt.c.attempt_id)
            .where(latest_attempt.c.task_id == task["task_id"])
            .order_by(latest_attempt.c.attempt_no.desc(), latest_attempt.c.attempt_id.desc())
            .limit(1)
            .scalar_subquery(),
            ~newer_task,
            ~pending_input,
            ~pending_request,
            ~native_owned,
        )
    )
    return statements.read(conn, query)


def analysis_actions(conn, result_id):
    actions, intents = tables["github_actions"], tables["publication_intents"]
    query = (
        select(
            actions.c.action_id,
            actions.c.task_id,
            actions.c.action_type,
            actions.c.audit_status,
            actions.c.publish_status,
            actions.c.target_url,
            actions.c.external_id,
            actions.c.created_at,
            actions.c.metadata_json,
            intents.c.status.label("intent_status"),
        )
        .select_from(actions.outerjoin(intents, intents.c.action_id == actions.c.action_id))
        .where(
            actions.c.result_id == result_id,
        )
        .order_by(actions.c.created_at, actions.c.action_id)
    )
    return statements.read(conn, query)
