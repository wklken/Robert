"""Select runnable source roots and materialize their exact coordinator state."""

from sqlalchemy import and_, func, or_, select, tuple_

from robert_agent.foundation.contracts.delivery import GitHubActionType, PublishStatus
from robert_agent.foundation.contracts.work import TaskKind
from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.expressions import valid_json_or_empty
from robert_agent.persistence.queries import source_github_ingress, source_results, sqlite_master


def progress(conn, source_id):
    t = tables["tapd_coordination"]
    return records.first(statements.read(conn, select(t).where(t.c.source_id == source_id)))


def _historical_delivery(source_id):
    """Keep canceled accepted roots observable until their local disposition settles."""
    root, round_, canceled, retired, recovery = (
        tables[name]
        for name in (
            "source_results",
            "source_rounds",
            "source_round_cancellations",
            "source_result_retirements",
            "source_delivery_recoveries",
        )
    )
    return (
        select(root.c.source_result_id)
        .select_from(root.join(round_, round_.c.round_id == root.c.round_id))
        .where(
            root.c.source_id == source_id,
            round_.c.source_id == source_id,
            round_.c.status == "canceled",
            select(1).where(canceled.c.round_id == round_.c.round_id).exists(),
            ~select(1).where(retired.c.source_result_id == root.c.source_result_id).exists(),
            ~select(1)
            .where(recovery.c.source_result_id == root.c.source_result_id, recovery.c.completed_at.is_not(None))
            .exists(),
        )
        .exists()
    )


def candidate(conn, now, owner, scopes, *, local_only=False):
    s, p, r, root, request, ledger, session, cancellation = (
        tables[name]
        for name in (
            "sources",
            "tapd_coordination",
            "source_rounds",
            "source_results",
            "ingress_requests",
            "request_events",
            "source_sessions",
            "source_round_cancellations",
        )
    )
    active = r.alias("active_native_round")
    result = select(root.c.source_result_id).where(root.c.round_id == active.c.round_id).exists()
    if not local_only and scopes and sqlite_master.table_exists(conn, "source_github_observations"):
        member = tables["source_round_requests"]
        mixed = (
            select(member.c.request_id)
            .select_from(member.join(request, request.c.request_id == member.c.request_id))
            .where(member.c.round_id == active.c.round_id, request.c.provider == "github")
            .exists()
        )
    else:
        mixed = False
    cancel_at = (
        select(cancellation.c.requested_at).where(cancellation.c.round_id == active.c.round_id).scalar_subquery()
    )
    canceled = cancel_at.is_not(None)
    pending = (
        select(request.c.request_id)
        .select_from(request.join(ledger, ledger.c.request_id == request.c.request_id))
        .where(request.c.source_id == s.c.source_id, request.c.phase == "pending", ledger.c.state == "pending")
        .exists()
    )
    github_pending = (
        source_github_ingress.pending_held(s.c.source_id).exists()
        if not local_only and scopes and sqlite_master.table_exists(conn, "source_github_observations")
        else False
    )
    historical_delivery = (
        _historical_delivery(s.c.source_id)
        if not local_only and scopes and sqlite_master.table_exists(conn, "source_result_retirements")
        else False
    )
    scoped = tuple_(s.c.host, s.c.project_key).in_(scopes)
    due = or_(
        p.c.source_id.is_(None),
        p.c.next_due_at <= now,
        and_(active.c.round_id.is_not(None), p.c.round_id != active.c.round_id),
        and_(canceled, cancel_at >= p.c.updated_at),
        and_(active.c.round_id.is_not(None), ~scoped, p.c.status != "draining", ~canceled),
    )
    notice = func.json_extract(valid_json_or_empty(s.c.metadata_json), "$.reason").in_(
        ("repository_conflict", "confirmation_required")
    )
    return records.first(
        statements.read(
            conn,
            select(s)
            .select_from(
                s.outerjoin(p, p.c.source_id == s.c.source_id)
                .outerjoin(active, and_(active.c.source_id == s.c.source_id, active.c.status == "active"))
                .outerjoin(session, session.c.source_id == s.c.source_id)
            )
            .where(
                s.c.provider == "tapd",
                due,
                True
                if owner is None
                else or_(
                    session.c.source_id.is_(None),
                    session.c.owner == owner,
                    func.julianday(session.c.lease_until) <= func.julianday(now),
                ),
                canceled
                if local_only
                else or_(
                    canceled,
                    and_(active.c.round_id.is_not(None), or_(~result, and_(mixed, scoped))),
                    and_(
                        active.c.round_id.is_(None),
                        or_(
                            pending,
                            notice,
                            source_results.pending_fixed_notice_results(s.c.source_id).exists(),
                            github_pending,
                            historical_delivery,
                        ),
                        scoped,
                    ),
                ),
            )
            .order_by(func.coalesce(p.c.updated_at, ""), s.c.source_id)
            .limit(1),
        )
    )


def tasks(conn, round_id):
    member, task, attempt = (tables[name] for name in ("source_round_tasks", "tasks", "attempts"))
    latest = (
        select(attempt.c.attempt_id)
        .where(attempt.c.task_id == task.c.task_id)
        .order_by(attempt.c.attempt_no.desc(), attempt.c.attempt_id)
        .limit(1)
        .correlate(task)
        .scalar_subquery()
    )
    return statements.read(
        conn,
        select(
            member,
            task.c.lifecycle,
            task.c.route_id,
            task.c.workstream_id,
            attempt.c.attempt_id,
            attempt.c.status.label("attempt_status"),
            attempt.c.metadata_json.label("attempt_metadata_json"),
        )
        .select_from(
            member.join(
                task, and_(task.c.task_id == member.c.task_id, task.c.task_kind == TaskKind.HUMAN_REQUEST)
            ).outerjoin(attempt, attempt.c.attempt_id == latest)
        )
        .where(member.c.round_id == round_id)
        .order_by(member.c.created_at, member.c.task_id),
    )


def has_pr_lineage(conn, source_id):
    stream, task, action = (tables[name] for name in ("workstreams", "tasks", "github_actions"))
    origin = stream.alias("native_origin")
    roots = select(origin.c.workstream_id).where(origin.c.primary_source_id == source_id)
    derived = select(stream.c.workstream_id).where(stream.c.origin_workstream_id.in_(roots))
    opened = (
        select(action.c.action_id)
        .select_from(action.join(task, task.c.task_id == action.c.task_id))
        .where(
            task.c.workstream_id.in_(roots),
            action.c.provider == "github",
            action.c.action_type == GitHubActionType.OPEN_PR,
            action.c.publish_status == PublishStatus.PUBLISHED,
        )
    )
    return bool(statements.read(conn, select(or_(derived.exists(), opened.exists())))[0][0])
