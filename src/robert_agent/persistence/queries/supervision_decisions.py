"""Bounded, read-only facts for an Issue/PR decision and its consumers."""

from sqlalchemy import select, func, or_

from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def _rows(conn, query):
    return [dict(row) for row in statements.read(conn, query)]


def stream(conn, workstream_id):
    table = tables["workstreams"]
    rows = _rows(conn, select(table).where(table.c.workstream_id == workstream_id))
    return rows[0] if rows else None


def task(conn, task_id):
    table = tables["tasks"]
    rows = _rows(conn, select(table).where(table.c.task_id == task_id))
    return rows[0] if rows else None


def root_stream(conn, workstream_id):
    seen = set()
    row = stream(conn, workstream_id)
    repo_id = row["repo_id"] if row else None
    while row and row["workstream_id"] not in seen and len(seen) < 16:
        if row["repo_id"] != repo_id:
            return None
        seen.add(row["workstream_id"])
        if not row["origin_workstream_id"]:
            return row
        row = stream(conn, row["origin_workstream_id"])
    return None


def family(conn, root_id):
    root = stream(conn, root_id)
    if not root:
        return []
    table = tables["workstreams"]
    rows, frontier = [root], [root_id]
    while frontier:
        children = _rows(
            conn,
            select(table)
            .where(table.c.origin_workstream_id.in_(frontier), table.c.repo_id == root["repo_id"])
            .order_by(table.c.workstream_id)
            .limit(33),
        )
        if len(rows) + len(children) > 32:
            raise ValueError("decision workstream family exceeds observation bound")
        seen = {row["workstream_id"] for row in rows}
        if any(row["workstream_id"] in seen for row in children):
            raise ValueError("cyclic workstream family")
        rows.extend(children)
        frontier = [row["workstream_id"] for row in children]
    return rows


def targets(conn, repositories, limit=100, after=""):
    ws, repo = tables["workstreams"], tables["repos"]
    task_table, source = tables["tasks"], tables["github_sources"]
    return _rows(
        conn,
        select(ws.c.workstream_id)
        .select_from(ws.join(repo, ws.c.repo_id == repo.c.repo_id))
        .where(
            ws.c.origin_workstream_id.is_(None),
            ws.c.workstream_id > after,
            ws.c.lifecycle != "canceled",
            repo.c.enabled == 1,
            repo.c.full_name.in_(repositories),
            or_(
                select(task_table.c.task_id).where(task_table.c.workstream_id == ws.c.workstream_id).exists(),
                select(source.c.source_id)
                .where(source.c.source_id == ws.c.primary_source_id, source.c.state == "open")
                .exists(),
            ),
        )
        .order_by(ws.c.workstream_id)
        .limit(limit),
    )


def checkpoints(conn, scope_type, scope_id, limit=100):
    cp = tables["supervision_checkpoints"]
    return _rows(
        conn,
        select(cp).where(cp.c.scope_type == scope_type, cp.c.scope_id == scope_id).order_by(cp.c.rule_id).limit(limit),
    )


def decision_states(conn, repositories):
    cp, ws, repo = tables["supervision_checkpoints"], tables["workstreams"], tables["repos"]
    return _rows(
        conn,
        select(cp.c.scope_id, cp.c.metadata_json)
        .select_from(cp.join(ws, ws.c.workstream_id == cp.c.scope_id).join(repo, repo.c.repo_id == ws.c.repo_id))
        .where(cp.c.scope_type == "decision", cp.c.rule_id == "current", repo.c.full_name.in_(repositories))
        .order_by(cp.c.updated_at.desc(), cp.c.scope_id)
        .limit(100),
    )


def _text(column):
    return func.substr(column, 1, 2048).label(column.name)


def context(conn, root_id):
    streams = family(conn, root_id)
    if not streams:
        return None
    ids = [row["workstream_id"] for row in streams]
    repo = tables["repos"]
    repository = _rows(
        conn, select(repo.c.repo_id, repo.c.full_name, repo.c.enabled).where(repo.c.repo_id == streams[0]["repo_id"])
    )[0]
    tasks = tables["tasks"]
    task_rows = _rows(
        conn,
        select(
            tasks.c.task_id,
            tasks.c.workstream_id,
            tasks.c.lifecycle,
            tasks.c.task_kind,
            tasks.c.parent_task_id,
            tasks.c.route_id,
            tasks.c.expected_output,
            tasks.c.created_at,
        )
        .where(tasks.c.workstream_id.in_(ids))
        .order_by(tasks.c.created_at.desc(), tasks.c.task_id.desc())
        .limit(16),
    )
    tids = [row["task_id"] for row in task_rows]
    attempts = tables["attempts"]
    attempt_rows = _rows(
        conn,
        select(
            attempts.c.attempt_id,
            attempts.c.task_id,
            attempts.c.status,
            attempts.c.started_at,
            attempts.c.finished_at,
            _text(attempts.c.failure_json),
            func.length(attempts.c.failure_json).label("failure_json_length"),
            func.json_extract(attempts.c.metadata_json, "$.process_stopped_at").label("process_stopped_at"),
        )
        .where(attempts.c.task_id.in_(tids))
        .order_by(attempts.c.started_at.desc(), attempts.c.attempt_id.desc())
        .limit(16),
    )
    sources, links = tables["github_sources"], tables["workstream_sources"]
    source_rows = _rows(
        conn,
        select(*[_text(c) if c.name in {"title", "metadata_json"} else c for c in sources.c])
        .where(
            or_(
                sources.c.source_id.in_([row["primary_source_id"] for row in streams if row["primary_source_id"]]),
                sources.c.source_id.in_(select(links.c.source_id).where(links.c.workstream_id.in_(ids))),
            )
        )
        .order_by(sources.c.source_id)
        .limit(16),
    )
    source_ids = [row["source_id"] for row in source_rows]
    events = tables["github_events"]
    event_rows = _rows(
        conn,
        select(
            events.c.event_id,
            events.c.event_type,
            events.c.actor_login,
            events.c.authorization_status,
            *[
                func.substr(func.json_extract(events.c.payload_json, "$." + key), 1, 2048).label(key)
                for key in (
                    "body",
                    "title",
                    "head_sha",
                    "base_sha",
                    "mergeable",
                    "state",
                    "conclusion",
                    "status",
                    "failure_signature",
                    "failure_summary",
                    "task_kind",
                    "intent",
                    "completed_at",
                )
            ],
            func.length(func.json_extract(events.c.payload_json, "$.body")).label("body_length"),
        )
        .where(events.c.source_id.in_(source_ids))
        .order_by(events.c.event_id.desc())
        .limit(16),
    )
    items = tables["work_items"]
    item_rows = _rows(
        conn,
        select(
            items.c.work_item_id,
            _text(items.c.title),
            func.length(items.c.title).label("title_length"),
            _text(items.c.description),
            func.length(items.c.description).label("description_length"),
            items.c.version,
            items.c.completed_at,
            items.c.canceled_at,
        )
        .where(items.c.workstream_id.in_(ids))
        .order_by(items.c.work_item_id)
        .limit(16),
    )
    item_events = tables["work_item_events"]
    replies = _rows(
        conn,
        select(
            item_events.c.event_id,
            item_events.c.event_type,
            _text(item_events.c.body),
            func.length(item_events.c.body).label("body_length"),
            item_events.c.resolves_event_id,
            item_events.c.created_at,
        )
        .where(item_events.c.work_item_id.in_([row["work_item_id"] for row in item_rows]))
        .order_by(item_events.c.created_at.desc(), item_events.c.event_id.desc())
        .limit(16),
    )
    episodes = tables["pr_remediation_episodes"]
    episode_rows = _rows(
        conn,
        select(*[_text(c) if c.name == "metadata_json" else c for c in episodes.c if c.name != "updated_at"])
        .where(episodes.c.workstream_id.in_(ids))
        .order_by(episodes.c.first_seen_at.desc(), episodes.c.episode_id.desc())
        .limit(16),
    )
    results = tables["worker_results"]
    result_rows = _rows(
        conn,
        select(
            results.c.result_id,
            results.c.attempt_id,
            results.c.task_id,
            results.c.output_type,
            results.c.created_at,
            _text(results.c.metadata_json),
            func.length(results.c.metadata_json).label("metadata_json_length"),
        )
        .where(results.c.task_id.in_(tids))
        .order_by(results.c.created_at.desc(), results.c.result_id.desc())
        .limit(16),
    )
    controls = tables["execution_subjects"]
    control_rows = _rows(
        conn,
        select(controls.c.subject_type, controls.c.subject_id, controls.c.binding_status, controls.c.recovery_attempts)
        .where(
            or_(controls.c.subject_id.in_(tids), controls.c.subject_id.in_([row["episode_id"] for row in episode_rows]))
        )
        .order_by(controls.c.subject_type, controls.c.subject_id)
        .limit(32),
    )
    cp = tables["supervision_checkpoints"]
    observations = _rows(
        conn,
        select(
            cp.c.scope_id,
            cp.c.rule_id,
            cp.c.classification,
            cp.c.reason_code,
            cp.c.progress_token,
            cp.c.last_progress_at,
            cp.c.eligible_since,
            _text(cp.c.evidence_json),
            func.length(cp.c.evidence_json).label("evidence_json_length"),
        )
        .where(cp.c.scope_type == "task", cp.c.scope_id.in_(tids))
        .order_by(cp.c.scope_id, cp.c.rule_id)
        .limit(64),
    )
    # Completion is an aggregate over the entire family, never a sampled claim.
    completion = _rows(
        conn,
        select(
            select(items.c.work_item_id).where(items.c.workstream_id.in_(ids)).exists().label("has_goal"),
            select(items.c.work_item_id)
            .where(
                items.c.workstream_id.in_(ids), or_(items.c.completed_at.is_(None), items.c.canceled_at.is_not(None))
            )
            .exists()
            .label("unfinished_goal"),
            select(tasks.c.task_id)
            .where(tasks.c.workstream_id.in_(ids), tasks.c.lifecycle.not_in(["completed", "canceled", "ignored"]))
            .exists()
            .label("unfinished_task"),
        ),
    )[0]
    return {
        "completion": completion,
        "repository": repository,
        "streams": [
            {k: row[k] for k in ("workstream_id", "origin_workstream_id", "lifecycle", "active_task_id")}
            for row in streams
        ],
        "goals": item_rows,
        "sources": source_rows,
        "events": event_rows,
        "user_events": replies,
        "tasks": task_rows,
        "attempts": attempt_rows,
        "episodes": episode_rows,
        "results": result_rows,
        "execution_controls": control_rows,
        "observations": observations,
    }
