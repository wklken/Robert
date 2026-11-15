"""Read TAPD candidate scope and bounded scan progress."""

from sqlalchemy import func, not_, or_, select, tuple_

from robert_agent.foundation.contracts.work import SourceIngressPhase, SourceRoundStatus
from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables


def discovery_progress(conn, keys):
    probe = tables["tapd_discovery_progress"]
    return {row["probe_key"]: row for row in statements.read(conn, select(probe).where(probe.c.probe_key.in_(keys)))}


def candidates(conn, scopes, *, configured, limit, outstanding_only=False):
    source, progress, request, round_, cancelled = (
        tables[name]
        for name in (
            "sources",
            "tapd_observation_progress",
            "ingress_requests",
            "source_rounds",
            "source_round_cancellations",
        )
    )
    matched = tuple_(source.c.host, source.c.project_key).in_(scopes)
    query = (
        select(source)
        .select_from(source.outerjoin(progress, progress.c.source_id == source.c.source_id))
        .where(source.c.provider == "tapd", matched if configured else not_(matched))
    )
    if outstanding_only:
        pending = (
            select(request.c.request_id)
            .where(
                request.c.source_id == source.c.source_id,
                request.c.phase.in_((SourceIngressPhase.PENDING.value, SourceIngressPhase.HELD.value)),
            )
            .exists()
        )
        active = (
            select(round_.c.round_id)
            .where(
                round_.c.source_id == source.c.source_id,
                round_.c.status == SourceRoundStatus.ACTIVE.value,
                ~select(cancelled.c.round_id).where(cancelled.c.round_id == round_.c.round_id).exists(),
            )
            .exists()
        )
        query = query.where(or_(pending, active))
    return statements.read(
        conn, query.order_by(func.coalesce(progress.c.attempted_at, ""), source.c.source_id).limit(limit)
    )


def source(conn, source_id):
    table = tables["sources"]
    return records.first(
        statements.read(conn, select(table).where(table.c.provider == "tapd", table.c.source_id == source_id))
    )


def observation_progress(conn, source_id):
    table = tables["tapd_observation_progress"]
    return records.first(statements.read(conn, select(table).where(table.c.source_id == source_id)))
