"""Materialized proposal observations and review history."""

from sqlalchemy import and_, case, func, or_, select

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables


def observation(conn, identity):
    proposal = tables["proposals"]
    rows = statements.read(conn, select(proposal).where(proposal.c.source_identity == identity))
    return dict(rows[0]) if rows else None


def current(conn, *, proposal_id=None, status=None, repo_ids=None, limit=100, ranked=False):
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
        roles = func.json_each(revision.c.source_json, "$.repository_candidates").table_valued("value")
        repo = tables["repos"]
        allowed = select(repo.c.full_name).where(repo.c.repo_id.in_(repo_ids))
        missing = select(1).select_from(roles).where(func.json_extract(roles.c.value, "$.repository").not_in(allowed))
        query = query.where(
            or_(
                proposal.c.repo_id.in_(repo_ids),
                and_(
                    proposal.c.repo_id.is_(None),
                    func.json_array_length(revision.c.source_json, "$.repository_candidates") > 0,
                    ~missing.correlate(revision).exists(),
                ),
            )
        )
    if ranked:
        recommendation = func.json_extract(revision.c.report_json, "$.recommendation")
        feasibility = func.json_extract(revision.c.report_json, "$.feasibility")
        query = query.where(or_(feasibility.is_(None), feasibility.in_(("ready", "investigate"))))
        query = query.order_by(case((recommendation == "high", 0), (recommendation == "medium", 1), else_=2))
    query = query.order_by(proposal.c.updated_at.desc(), proposal.c.proposal_id).limit(limit)
    return [dict(row) for row in statements.read(conn, query)]


def daily_candidates(conn):
    proposal, revision = tables["proposals"], tables["proposal_revisions"]
    query = (
        select(proposal, revision.c.source_json, revision.c.report_json)
        .join(
            revision,
            (revision.c.proposal_id == proposal.c.proposal_id) & (revision.c.revision == proposal.c.revision),
        )
        .where(proposal.c.status.in_(("pending", "blocked")))
    )
    return [dict(row) for row in statements.read(conn, query)]


def expired_holds(conn, now):
    proposal = tables["proposals"]
    query = select(proposal).where(
        proposal.c.status == "held", func.julianday(proposal.c.defer_until) <= func.julianday(now)
    )
    return [dict(row) for row in statements.read(conn, query)]


def scan_result(conn, slot):
    scans = tables["proposal_scans"]
    row = statements.read(conn, select(scans).where(scans.c.slot == slot))
    return dict(row[0]) if row else None


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


def known_tapd_source(conn, identity):
    source, stream, item = (tables[name] for name in ("github_sources", "workstreams", "work_items"))
    query = (
        select(source.c.source_id)
        .where(
            source.c.source_key.startswith(identity + ":", autoescape=True),
            or_(
                select(1).where(stream.c.primary_source_id == source.c.source_id).exists(),
                select(1).where(item.c.origin_source_id == source.c.source_id).exists(),
            ),
        )
        .limit(1)
    )
    return bool(statements.read(conn, query))


def formal_work_exists(conn, workstream_id):
    task, link, event = (tables[name] for name in ("tasks", "task_events", "github_events"))
    origin = task.alias("proposal_investigation")
    investigation = (
        select(1)
        .select_from(
            origin.join(link, origin.c.task_id == link.c.task_id).join(event, link.c.event_id == event.c.event_id)
        )
        .where(
            same_task_lineage(task, origin),
            event.c.event_type == "proposal_approved",
            func.json_extract(event.c.payload_json, "$.proposal_action") == "investigate",
        )
        .correlate(task)
    )
    query = select(task.c.task_id).where(task.c.workstream_id == workstream_id, ~investigation.exists()).limit(1)
    return bool(statements.read(conn, query))


def finished_investigations(conn, repo_id):
    proposal, task, action, stream = (tables[name] for name in ("proposals", "tasks", "github_actions", "workstreams"))
    root = task.alias("investigation_root")
    latest = (
        select(task.c.task_id)
        .where(same_task_lineage(task, root))
        .order_by(task.c.created_at.desc(), task.c.task_id.desc())
        .limit(1)
        .correlate(root)
        .scalar_subquery()
    )
    delivered = (
        select(action.c.action_id)
        .where(
            action.c.task_id == task.c.task_id,
            action.c.action_type == "comment",
            action.c.audit_status == "accepted",
            action.c.publish_status == "published",
        )
        .correlate(task)
    )
    query = (
        select(proposal, task.c.lifecycle.label("investigation_outcome"))
        .select_from(
            proposal.join(root, root.c.task_id == proposal.c.investigation_task_id)
            .join(task, task.c.task_id == latest)
            .join(stream, stream.c.workstream_id == proposal.c.workstream_id)
        )
        .where(
            proposal.c.repo_id == repo_id,
            proposal.c.status == "investigating",
            stream.c.active_task_id.is_(None),
            or_(
                and_(task.c.lifecycle == "completed", delivered.exists()),
                task.c.lifecycle.in_(("failed", "canceled")),
            ),
        )
    )
    return [dict(row) for row in statements.read(conn, query)]


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
