"""Read frozen native input, repository ownership and exact Worker bindings."""

from sqlalchemy import and_, func, select

from robert_agent.foundation.contracts.delivery import AuditStatus, GitHubActionType, PublicationStatus, PublishStatus
from robert_agent.foundation.contracts.work import RequestEventState
from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.expressions import valid_json_or_empty
from robert_agent.persistence.queries import request_events


def round_trigger_events(conn, round_id):
    """Return the frozen request's real trigger IDs and provider fingerprints."""
    fields = ("request_id", "event_id", "event_fingerprint")
    return [
        records.Record(fields, tuple(row[field] for field in fields))
        for row in request_events.source_round_events(conn, round_id)
        if row["event_id"] == row["frozen_event_id"] and row["state"] == RequestEventState.PENDING.value
    ]


def trigger_bindings(conn, task_id):
    table = tables["task_events"]
    return frozenset(
        row[0]
        for row in statements.read(
            conn, select(table.c.event_id).where(table.c.task_id == task_id, table.c.relationship == "trigger")
        )
    )


def repository(conn, repo_id):
    table = tables["repos"]
    return records.first(statements.read(conn, select(table).where(table.c.repo_id == repo_id, table.c.enabled == 1)))


def has_native_origin(conn, task_id):
    task, stream, source, member = (tables[name] for name in ("tasks", "workstreams", "sources", "source_round_tasks"))
    origin = stream.alias("origin")
    tapd = (
        select(task.c.task_id)
        .select_from(
            task.join(stream, stream.c.workstream_id == task.c.workstream_id)
            .outerjoin(origin, origin.c.workstream_id == stream.c.origin_workstream_id)
            .join(source, source.c.source_id.in_((stream.c.primary_source_id, origin.c.primary_source_id)))
        )
        .where(task.c.task_id == task_id, source.c.provider == "tapd")
    )
    bound = select(member.c.task_id).where(member.c.task_id == task_id)
    return bool(statements.read(conn, bound.union(tapd)))


def attempt(conn, task_id, attempt_id):
    table = tables["attempts"]
    return records.first(
        statements.read(conn, select(table).where(table.c.task_id == task_id, table.c.attempt_id == attempt_id))
    )


def is_latest_attempt(conn, task_id, attempt_no):
    table = tables["attempts"]
    return not statements.read(
        conn, select(table.c.attempt_id).where(table.c.task_id == task_id, table.c.attempt_no > attempt_no).limit(1)
    )


def confirmed_pr(conn, *, source_id, repo_id, workstream_id, action_id):
    """Use an accepted open-PR receipt from the exact native repository origin."""
    stream, source, task, action, intent = (
        tables[name] for name in ("workstreams", "sources", "tasks", "github_actions", "publication_intents")
    )
    origin = stream.alias("pr_origin")
    receipt = func.json_extract(valid_json_or_empty(intent.c.remote_evidence_json), "$.receipt")
    return records.first(
        statements.read(
            conn,
            select(
                source.c.host,
                source.c.project_key,
                source.c.number,
                source.c.state,
                source.c.html_url,
                action.c.target_url,
                receipt.label("receipt_json"),
                origin.c.workstream_id.label("origin_workstream_id"),
            )
            .select_from(
                stream.join(origin, origin.c.workstream_id == stream.c.origin_workstream_id)
                .join(source, source.c.source_id == stream.c.primary_source_id)
                .join(task, task.c.workstream_id == origin.c.workstream_id)
                .join(action, action.c.task_id == task.c.task_id)
                .join(intent, intent.c.action_id == action.c.action_id)
            )
            .where(
                stream.c.workstream_id == workstream_id,
                stream.c.repo_id == repo_id,
                origin.c.repo_id == repo_id,
                origin.c.primary_source_id == source_id,
                source.c.repo_id == repo_id,
                source.c.provider == "github",
                source.c.source_type == "pull_request",
                action.c.action_id == action_id,
                action.c.provider == "github",
                action.c.action_type == GitHubActionType.OPEN_PR.value,
                action.c.audit_status == AuditStatus.ACCEPTED.value,
                action.c.publish_status == PublishStatus.PUBLISHED.value,
                intent.c.status == PublicationStatus.PUBLISHED.value,
            ),
        )
    )


def stream_ready(conn, workstream_id):
    stream, task = tables["workstreams"], tables["tasks"]
    return records.first(
        statements.read(
            conn,
            select(stream.c.lifecycle, task.c.lifecycle.label("task_lifecycle"))
            .select_from(
                stream.outerjoin(
                    task,
                    and_(task.c.task_id == stream.c.active_task_id, task.c.workstream_id == stream.c.workstream_id),
                )
            )
            .where(stream.c.workstream_id == workstream_id),
        )
    )
