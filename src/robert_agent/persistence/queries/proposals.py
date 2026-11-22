"""Materialized proposal observations and review history."""

from sqlalchemy import func, or_, select

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables


def observation(conn, identity):
    proposal = tables["proposals"]
    rows = statements.read(conn, select(proposal).where(proposal.c.source_identity == identity))
    return dict(rows[0]) if rows else None


def current(conn, *, proposal_id=None, status=None, repo_ids=None, limit=100):
    proposal, revision = tables["proposals"], tables["proposal_revisions"]
    query = select(proposal, revision.c.source_json, revision.c.report_json).join(
        revision,
        (revision.c.proposal_id == proposal.c.proposal_id) & (revision.c.revision == proposal.c.revision),
    )
    if proposal_id is not None:
        query = query.where(proposal.c.proposal_id == proposal_id)
    if status is not None:
        query = query.where(proposal.c.status == status)
    if repo_ids is not None:
        query = query.where(proposal.c.repo_id.in_(repo_ids))
    query = query.order_by(proposal.c.updated_at.desc(), proposal.c.proposal_id).limit(limit)
    return [dict(row) for row in statements.read(conn, query)]


def decision_history(conn, proposal_id):
    decision = tables["proposal_decisions"]
    query = select(decision).where(decision.c.proposal_id == proposal_id).order_by(decision.c.version)
    return [dict(row) for row in statements.read(conn, query)]


def scan_completed(conn, slot):
    scans = tables["proposal_scans"]
    return bool(statements.read(conn, select(scans.c.slot).where(scans.c.slot == slot)))


def known_source(conn, source_keys):
    stream = tables["workstreams"]
    item = tables["work_items"]
    source = tables["github_sources"]
    work_key = select(stream.c.workstream_id).where(stream.c.workstream_id.in_(source_keys)).limit(1)
    item_origin = (
        select(item.c.work_item_id)
        .join(source, source.c.source_id == item.c.origin_source_id)
        .where(source.c.source_key.in_(source_keys))
        .limit(1)
    )
    return bool(statements.read(conn, work_key) or statements.read(conn, item_origin))


def get_decision(conn, proposal_id, idempotency_key=None, version=None):
    table = tables["proposal_decisions"]
    query = select(table).where(table.c.proposal_id == proposal_id)
    if idempotency_key is not None:
        query = query.where(table.c.idempotency_key == idempotency_key)
    if version is not None:
        query = query.where(table.c.version == version)
    rows = statements.read(conn, query.order_by(table.c.version.desc()).limit(1))
    return dict(rows[0]) if rows else None


def work_binding(conn, workstream_id):
    stream, item = tables["workstreams"], tables["work_items"]
    query = (
        select(stream.c.workstream_id, stream.c.active_task_id, item.c.work_item_id)
        .outerjoin(item, item.c.workstream_id == stream.c.workstream_id)
        .where(stream.c.workstream_id == workstream_id)
    )
    rows = statements.read(conn, query)
    return dict(rows[0]) if rows else None


def task_bound(conn, task_id):
    """A proposal-bound task requires current configuration even on direct entrypoints."""
    task, binding, event = (tables[name] for name in ("tasks", "task_events", "github_events"))
    query = (
        select(event.c.event_id)
        .select_from(
            task.join(binding, binding.c.task_id == task.c.task_id).join(event, event.c.event_id == binding.c.event_id)
        )
        .where(
            task.c.task_id == task_id,
            event.c.event_type == "proposal_approved",
            or_(
                binding.c.relationship == "trigger",
                event.c.event_fingerprint == func.json_extract(task.c.metadata_json, "$.request_event_fingerprint"),
            ),
        )
        .limit(1)
    )
    return bool(statements.read(conn, query))


def same_task_lineage(task, root):
    lineage = func.json_extract(root.c.metadata_json, "$.supervision_lineage_key")
    return or_(
        task.c.task_id == root.c.task_id,
        (lineage.is_not(None)) & (func.json_extract(task.c.metadata_json, "$.supervision_lineage_key") == lineage),
    )


def work_progress(conn, task_id):
    tasks = tables["tasks"]
    root = tasks.alias("proposal_root")
    query = (
        select(tasks.c.task_id, tasks.c.lifecycle, tasks.c.updated_at)
        .select_from(root.join(tasks, same_task_lineage(tasks, root)))
        .where(root.c.task_id == task_id)
        .order_by(tasks.c.created_at.desc(), tasks.c.task_id.desc())
        .limit(1)
    )
    rows = statements.read(conn, query)
    return dict(rows[0]) if rows else None
