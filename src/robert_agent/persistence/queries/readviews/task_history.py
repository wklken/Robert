"""Task and execution evidence used by the board, dashboard and workbench."""

from robert_agent.foundation.contracts.execution import AttemptStatus

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import case, select

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.predicates import occupied_attempt


def _for_ids(conn, query, key, ids):
    if ids is None:
        return statements.read(conn, query)
    keys = tuple(dict.fromkeys(ids))
    return [
        row
        for start in range(0, len(keys), 200)
        for row in statements.read(conn, query.where(key.in_(keys[start : start + 200])))
    ]


def tasks_for_workstreams(conn, workstream_ids, *, chronological=False):
    task = tables["tasks"].c
    order = (task.created_at, task.task_id) if chronological else (task.updated_at.desc(), task.task_id.desc())
    query = select(
        task.task_id,
        task.workstream_id,
        task.lifecycle,
        task.parent_task_id,
        task.priority,
        task.route_id,
        task.expected_output,
        task.created_at,
        task.updated_at,
        task.metadata_json,
        task.routing_mode,
        task.requested_worker,
    ).order_by(*order)
    if chronological:
        # Board detail displays one chronology across all related workstreams.
        if workstream_ids is not None:
            query = query.where(task.workstream_id.in_(workstream_ids))
        return statements.read(conn, query)
    return _for_ids(conn, query, task.workstream_id, workstream_ids)


def attempts_for_tasks(conn, task_ids):
    attempt = tables["attempts"].c
    query = select(
        attempt.attempt_id,
        attempt.task_id,
        attempt.attempt_no,
        attempt.status,
        attempt.worktree_path,
        attempt.branch_name,
        attempt.started_at,
        attempt.heartbeat_at,
        attempt.finished_at,
        attempt.failure_json,
    ).order_by(attempt.attempt_no.desc(), attempt.started_at.desc(), attempt.attempt_id.desc())
    return _for_ids(conn, query, attempt.task_id, task_ids)


def attempt_timing_for_task(conn, task_id):
    attempt = tables["attempts"].c
    query = (
        select(
            attempt.attempt_id,
            attempt.status,
            attempt.started_at,
            attempt.finished_at,
            attempt.metadata_json,
        )
        .where(attempt.task_id == task_id)
        .order_by(attempt.attempt_no)
    )
    return statements.read(conn, query)


def phases_for_attempts(conn, attempt_ids):
    phase = tables["worker_phases"].c
    query = select(
        phase.phase_id,
        phase.attempt_id,
        phase.phase,
        phase.status,
        phase.summary,
        phase.next_step,
        phase.created_at,
    ).order_by(phase.created_at, phase.phase_id)
    return _for_ids(conn, query, phase.attempt_id, attempt_ids)


def results_for_tasks(conn, task_ids):
    result = tables["worker_results"].c
    query = select(
        result.result_id,
        result.task_id,
        result.attempt_id,
        result.output_type,
        result.verification_json,
        result.handoff,
        result.created_at,
        result.metadata_json,
    ).order_by(result.created_at.desc(), result.result_id.desc())
    return _for_ids(conn, query, result.task_id, task_ids)


def result_timing_for_task(conn, task_id):
    result = tables["worker_results"].c
    query = (
        select(result.attempt_id, result.created_at, result.metadata_json)
        .where(
            result.task_id == task_id,
        )
        .order_by(result.created_at)
    )
    return statements.read(conn, query)


def completed_attempt_results(conn, task_ids):
    results, attempts = tables["worker_results"], tables["attempts"]
    query = (
        select(results.c.task_id, results.c.metadata_json)
        .select_from(results.join(attempts, attempts.c.attempt_id == results.c.attempt_id))
        .where(attempts.c.status == sql_constant(AttemptStatus.COMPLETED.value))
        .order_by(results.c.created_at.desc(), results.c.result_id.desc())
    )
    return _for_ids(conn, query, results.c.task_id, task_ids)


def actions_for_tasks(conn, task_ids):
    action = tables["github_actions"].c
    query = select(
        action.action_id,
        action.result_id,
        action.task_id,
        action.action_type,
        action.target_url,
        action.external_id,
        action.audit_status,
        action.publish_status,
        action.created_at,
        action.metadata_json,
    ).order_by(action.created_at.desc(), action.action_id.desc())
    return _for_ids(conn, query, action.task_id, task_ids)


def artifacts_for_tasks(conn, task_ids=None, *, attempt_id=None):
    artifact = tables["artifacts"].c
    query = select(
        artifact.artifact_id,
        artifact.task_id,
        artifact.attempt_id,
        artifact.artifact_type,
        artifact.path,
        artifact.bytes,
        artifact.created_at,
    ).order_by(artifact.created_at.desc(), artifact.artifact_id.desc())
    if attempt_id:
        query = query.where(artifact.attempt_id == attempt_id)
    return _for_ids(conn, query, artifact.task_id, task_ids)


def notifications_for_tasks(conn, task_ids):
    notification = tables["notifications"].c
    query = select(
        notification.notification_id,
        notification.task_id,
        notification.notification_type,
        notification.channel,
        notification.status,
        notification.created_at,
        notification.metadata_json,
    ).order_by(notification.created_at.desc(), notification.notification_id.desc())
    return _for_ids(conn, query, notification.task_id, task_ids)


def remediation_for_workstreams(conn, workstream_ids):
    episode = tables["pr_remediation_episodes"].c
    query = select(
        episode.episode_id,
        episode.workstream_id,
        episode.episode_kind,
        episode.status,
        episode.observed_head_sha,
        episode.observed_base_sha,
        episode.result_head_sha,
        episode.attempt_count,
        episode.failure_signature,
        episode.first_seen_at,
        episode.updated_at,
        episode.terminal_at,
        episode.last_task_id,
        episode.metadata_json,
    ).order_by(episode.updated_at.desc(), episode.episode_id.desc())
    return _for_ids(conn, query, episode.workstream_id, workstream_ids)


def occupied_tasks(conn, workstream_ids):
    attempts, tasks = tables["attempts"], tables["tasks"]
    query = (
        select(attempts.c.task_id)
        .select_from(attempts.join(tasks, tasks.c.task_id == attempts.c.task_id))
        .where(occupied_attempt(attempts))
    )
    return {row[0] for row in _for_ids(conn, query, tasks.c.workstream_id, workstream_ids)}


def event_sources_for_tasks(conn, task_ids):
    links, events, sources = (
        tables["task_events"],
        tables["github_events"],
        tables["github_sources"],
    )
    preference = case(
        {"trigger": 0, "context": 1, "pending": 2, "consumed": 3},
        value=links.c.relationship,
        else_=4,
    )
    query = (
        select(
            links.c.task_id,
            events.c.event_fingerprint,
            sources.c.source_key,
            sources.c.title.label("source_title"),
            sources.c.html_url,
            sources.c.source_type,
            sources.c.number,
        )
        .select_from(
            links.join(events, events.c.event_id == links.c.event_id).join(
                sources,
                sources.c.source_id == events.c.source_id,
            )
        )
        .order_by(preference, links.c.created_at.desc())
    )
    return _for_ids(conn, query, links.c.task_id, task_ids)


def primary_sources_for_tasks(conn, task_ids):
    tasks, workstreams, sources = (
        tables["tasks"],
        tables["workstreams"],
        tables["github_sources"],
    )
    query = select(
        tasks.c.task_id,
        sources.c.source_key,
        sources.c.title.label("source_title"),
        sources.c.html_url,
        sources.c.source_type,
        sources.c.number,
    ).select_from(
        tasks.join(workstreams, workstreams.c.workstream_id == tasks.c.workstream_id).join(
            sources,
            sources.c.source_id == workstreams.c.primary_source_id,
        )
    )
    return _for_ids(conn, query, tasks.c.task_id, task_ids)


def metadata_for_tasks(conn, task_ids):
    task = tables["tasks"].c
    return _for_ids(conn, select(task.task_id, task.metadata_json), task.task_id, task_ids)
