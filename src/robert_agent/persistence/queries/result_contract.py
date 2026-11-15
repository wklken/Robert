"""Legacy result contract evidence used by the cutover guard."""

from robert_agent.foundation.contracts.delivery import AuditStatus, PublishStatus
from robert_agent.foundation.contracts.execution import AttemptStatus
from robert_agent.foundation.contracts.work import TaskLifecycle, WorkstreamLifecycle

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, and_, or_, func


from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.dialects.sqlite import table_info
from robert_agent.persistence.queries import source_members


def legacy_evidence(conn, contract_ids):
    tasks, attempts = tables["tasks"], tables["attempts"]
    results, streams, actions = (
        tables["worker_results"],
        tables["workstreams"],
        tables["github_actions"],
    )
    task, attempt, result = tasks.c, attempts.c, results.c
    unfinished = task.lifecycle.in_(
        (sql_constant(TaskLifecycle.QUEUED.value), sql_constant(TaskLifecycle.RUNNING.value))
    )

    attempt_query = (
        select(
            attempt.attempt_id,
            attempt.task_id,
            attempt.status,
            attempt.metadata_json,
            task.lifecycle,
        )
        .select_from(attempts.join(tasks, task.task_id == attempt.task_id))
        .where(
            or_(
                attempt.status.in_(
                    (sql_constant(AttemptStatus.RUNNING.value), sql_constant(AttemptStatus.STALE.value))
                ),
                and_(attempt.status == AttemptStatus.PREPARED, unfinished),
            ),
            func.coalesce(
                func.json_extract(attempt.metadata_json, sql_constant("$.result_contract")),
                sql_constant(""),
            ).not_in(contract_ids),
        )
    )

    # The uncorrelated sets avoid rescanning the terminal history for each result.
    active_tasks = select(streams.c.active_task_id).where(
        streams.c.lifecycle == sql_constant(WorkstreamLifecycle.ACTIVE.value)
    )
    pending_results = select(actions.c.result_id).where(
        actions.c.publish_status == sql_constant(PublishStatus.NOT_PUBLISHED.value),
        actions.c.audit_status.in_((sql_constant(AuditStatus.PENDING.value), sql_constant(AuditStatus.ACCEPTED.value))),
    )
    result_query = (
        select(result.result_id, result.task_id)
        .select_from(results.join(tasks, task.task_id == result.task_id))
        .where(
            func.coalesce(
                func.json_extract(result.metadata_json, sql_constant("$.result_contract")),
                sql_constant(""),
            ).not_in(contract_ids),
            or_(
                unfinished,
                task.task_id.in_(active_tasks),
                result.result_id.in_(pending_results),
            ),
        )
    )

    # Databases from before task metadata still need the legacy startup check.
    if any(column[1] == "metadata_json" for column in table_info(conn, "tasks")):
        unisolated = func.coalesce(
            func.json_extract(task.metadata_json, sql_constant("$.execution_attention.reason_code")),
            sql_constant(""),
        ) != ("result_contract_cutover")
        attempt_query = attempt_query.where(unisolated)
        result_query = result_query.where(unisolated)
    return statements.read(conn, attempt_query), statements.read(conn, result_query)


def task_round_binding(conn, task_id):
    """Return source-round membership and identities from the owning tables."""
    member, round_, frozen, request, source, task, stream, repo = (
        tables[name]
        for name in (
            "source_round_tasks",
            "source_rounds",
            "source_round_requests",
            "ingress_requests",
            "sources",
            "tasks",
            "workstreams",
            "repos",
        )
    )
    origin = stream.alias("origin_stream")
    primary = source.alias("primary_source")
    row = records.first(
        statements.read(
            conn,
            select(
                member.c.round_id,
                member.c.request_id,
                member.c.repo_id,
                member.c.capability,
                round_.c.status,
                round_.c.capability.label("round_capability"),
                round_.c.repo_ids_json,
                round_.c.publication_scope_json,
                round_.c.source_id,
                round_.c.input_sha256,
                round_.c.input_version,
                source.c.host,
                source.c.project_key,
                source.c.source_type,
                source.c.external_id,
                task.c.route_id,
                repo.c.full_name,
                primary.c.provider.label("primary_provider"),
                primary.c.host.label("primary_host"),
                primary.c.project_key.label("primary_project"),
                primary.c.source_type.label("primary_kind"),
                primary.c.external_id.label("primary_external_id"),
            )
            .select_from(
                member.join(round_, round_.c.round_id == member.c.round_id)
                .join(frozen, and_(frozen.c.round_id == member.c.round_id, frozen.c.request_id == member.c.request_id))
                .join(request, request.c.request_id == member.c.request_id)
                .join(source, source.c.source_id == round_.c.source_id)
                .join(task, task.c.task_id == member.c.task_id)
                .join(stream, stream.c.workstream_id == task.c.workstream_id)
                .join(repo, repo.c.repo_id == stream.c.repo_id)
                .join(primary, primary.c.source_id == stream.c.primary_source_id)
                .outerjoin(origin, origin.c.workstream_id == stream.c.origin_workstream_id)
            )
            .where(
                member.c.task_id == task_id,
                source.c.provider == "tapd",
                source.c.source_type.in_(("story", "bug")),
                member.c.repo_id == repo.c.repo_id,
                or_(request.c.repo_id.is_(None), request.c.repo_id == repo.c.repo_id),
                or_(
                    stream.c.primary_source_id == source.c.source_id,
                    and_(
                        primary.c.provider == "github",
                        primary.c.source_type == "pull_request",
                        primary.c.repo_id == repo.c.repo_id,
                        origin.c.repo_id == repo.c.repo_id,
                        origin.c.primary_source_id == source.c.source_id,
                    ),
                ),
            ),
        )
    )

    if row is None or source_members.owned_request(conn, row["source_id"], row["request_id"]) is None:
        return None
    return row


def has_round_membership(conn, task_id):
    member = tables["source_round_tasks"]
    return bool(statements.read(conn, select(member.c.task_id).where(member.c.task_id == task_id)))
