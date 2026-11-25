"""Bounded task selection and batched evidence for operator inspection."""

from sqlalchemy import func, literal, or_, select, union_all

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries import sqlite_master


ROUND_TABLES = (
    ("review", "review_sessions", "review_runs"),
    ("vote", "vote_sessions", "vote_runs"),
    ("pre_pr_review", "pre_pr_review_sessions", "pre_pr_review_runs"),
)


def _task_selection():
    tasks, streams, repos, sources, attempts = (
        tables[name] for name in ("tasks", "workstreams", "repos", "github_sources", "attempts")
    )
    candidate = attempts.alias("candidate")
    latest = (
        select(candidate.c.attempt_id)
        .where(candidate.c.task_id == tasks.c.task_id)
        .order_by(candidate.c.attempt_no.desc(), candidate.c.started_at.desc(), candidate.c.attempt_id.desc())
        .limit(1)
        .correlate(tasks)
        .scalar_subquery()
    )
    return select(
        tasks,
        streams.c.public_ref,
        streams.c.lifecycle.label("workstream_lifecycle"),
        repos.c.full_name.label("repo"),
        sources.c.source_key,
        sources.c.title,
        sources.c.html_url,
        attempts.c.attempt_id,
        attempts.c.attempt_no,
        attempts.c.status.label("attempt_status"),
        attempts.c.started_at,
        attempts.c.heartbeat_at,
        attempts.c.finished_at,
        attempts.c.failure_json,
        attempts.c.metadata_json.label("attempt_metadata_json"),
    ).select_from(
        tasks.join(streams, streams.c.workstream_id == tasks.c.workstream_id)
        .join(repos, repos.c.repo_id == streams.c.repo_id)
        .outerjoin(sources, sources.c.source_id == streams.c.primary_source_id)
        .outerjoin(attempts, attempts.c.attempt_id == latest)
    )


def list_tasks(conn, *, states=(), repo=None, query=None, since=None, latest=False, limit=50, offset=0):
    tasks, streams, repos, sources = (tables[name] for name in ("tasks", "workstreams", "repos", "github_sources"))
    selection = _task_selection()
    if latest:
        newer = tasks.alias("newer")
        selection = selection.where(
            ~select(newer.c.task_id)
            .where(
                newer.c.workstream_id == tasks.c.workstream_id,
                or_(
                    func.julianday(newer.c.created_at) > func.julianday(tasks.c.created_at),
                    (func.julianday(newer.c.created_at) == func.julianday(tasks.c.created_at))
                    & (newer.c.task_id > tasks.c.task_id),
                ),
            )
            .exists()
        )
    if states:
        selection = selection.where(tasks.c.lifecycle.in_(states))
    if repo:
        selection = selection.where(repos.c.full_name == repo)
    if since:
        selection = selection.where(func.julianday(tasks.c.created_at) >= func.julianday(since))
    if query:
        selection = selection.where(
            or_(
                *(
                    column.contains(query, autoescape=True)
                    for column in (
                        tasks.c.task_id,
                        tasks.c.workstream_id,
                        streams.c.public_ref,
                        sources.c.source_key,
                        sources.c.title,
                    )
                )
            )
        )
    return statements.read(
        conn,
        selection.order_by(func.julianday(tasks.c.created_at).desc(), tasks.c.task_id.desc())
        .limit(limit + 1)
        .offset(offset),
    )


def task(conn, task_id):
    return statements.read(conn, _task_selection().where(tables["tasks"].c.task_id == task_id))


def lifecycle_counts(conn):
    task = tables["tasks"].c
    return {
        row[0]: row[1] for row in statements.read(conn, select(task.lifecycle, func.count()).group_by(task.lifecycle))
    }


def _available_rounds(conn):
    return [
        (kind, tables[session], tables[run])
        for kind, session, run in ROUND_TABLES
        if sqlite_master.table_exists(conn, session) and sqlite_master.table_exists(conn, run)
    ]


def active_runs(conn, attempt_ids):
    if not attempt_ids:
        return []
    result = []
    attempts = tables["attempts"]
    for kind, session, run in _available_rounds(conn):
        selection = (
            select(run, session.c.task_id.label("round_task_id"), attempts.c.status.label("owner_attempt_status"))
            .join(session, session.c.round_id == run.c.round_id)
            .join(attempts, attempts.c.attempt_id == run.c.owner_attempt_id)
            .where(
                run.c.owner_attempt_id.in_(attempt_ids),
                attempts.c.status.in_(("running", "stale")),
                run.c.status.in_(("prepared", "waiting", "starting", "running")),
            )
            .order_by(run.c.created_at, run.c.run_id)
        )
        result.extend(dict(row) | {"kind": kind} for row in statements.read(conn, selection))
    return result


def stages(conn, task_id, *, attempt_id=None, limit=50, offset=0):
    queries = []
    attempts = tables["attempts"]
    for kind, session, run in _available_rounds(conn):
        # Historical rounds can be rebound to a new task. Retain both the round
        # association and the original owner, never relabel a historical run.
        selection = (
            select(
                run,
                session.c.task_id.label("round_task_id"),
                attempts.c.task_id.label("owner_task_id"),
                attempts.c.status.label("owner_attempt_status"),
            )
            .select_from(
                run.join(session, session.c.round_id == run.c.round_id).join(
                    attempts, attempts.c.attempt_id == run.c.owner_attempt_id
                )
            )
            .where(or_(session.c.task_id == task_id, attempts.c.task_id == task_id))
        )
        if attempt_id is not None:
            selection = selection.where(run.c.owner_attempt_id == attempt_id)
        queries.append(selection.add_columns(literal(kind).label("kind")))
    if not queries:
        return []
    combined = union_all(*queries).subquery()
    return statements.read(
        conn,
        select(combined)
        .order_by(func.julianday(combined.c.created_at).desc(), combined.c.run_id.desc())
        .limit(limit + 1)
        .offset(offset),
    )


def snapshots(conn, task_id, *, limit=50, offset=0):
    snapshot, session, run, attempt = (
        tables[name] for name in ("pre_pr_review_snapshots", "pre_pr_review_sessions", "pre_pr_review_runs", "attempts")
    )
    if not sqlite_master.table_exists(conn, snapshot.name):
        return []
    owned_round = (
        select(run.c.round_id)
        .join(attempt, attempt.c.attempt_id == run.c.owner_attempt_id)
        .where(attempt.c.task_id == task_id)
    )
    query = (
        select(snapshot, session.c.task_id.label("round_task_id"), session.c.snapshot_id.label("current_snapshot_id"))
        .join(session, session.c.round_id == snapshot.c.round_id)
        .where(or_(session.c.task_id == task_id, snapshot.c.round_id.in_(owned_round)))
    )
    return statements.read(
        conn,
        query.order_by(func.julianday(snapshot.c.created_at).desc(), snapshot.c.snapshot_id.desc())
        .limit(limit + 1)
        .offset(offset),
    )


def manifests(conn, task_id):
    result = []
    for kind, session, _ in _available_rounds(conn):
        if kind == "pre_pr_review":
            continue
        result.extend(
            dict(row) | {"kind": kind}
            for row in statements.read(
                conn,
                select(session.c.round_id, session.c.task_id, session.c.manifest_json, session.c.created_at).where(
                    session.c.task_id == task_id
                ),
            )
        )
    return result


def usage_for_runs(conn, run_ids):
    if not run_ids or not sqlite_master.table_exists(conn, "model_usage"):
        return []
    usage = tables["model_usage"]
    return statements.read(conn, select(usage).where(usage.c.kind == "stage", usage.c.call_id.in_(run_ids)))


def stderr_artifacts(conn, task_id, attempt_id):
    artifact = tables["artifacts"]
    return statements.read(
        conn,
        select(artifact)
        .where(
            artifact.c.task_id == task_id,
            artifact.c.attempt_id == attempt_id,
            artifact.c.artifact_type == "worker_stderr",
        )
        .order_by(artifact.c.created_at.desc(), artifact.c.artifact_id.desc())
        .limit(1),
    )
