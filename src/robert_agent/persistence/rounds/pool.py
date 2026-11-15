"""One SQLite admission limit shared by native review, voting and source model processes."""

from robert_agent.foundation.contracts.execution import ModelRunStatus
from robert_agent.foundation.contracts.work import SourceRoundStatus
from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, func, update
from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements
from robert_agent.persistence.dialects import sqlite as db_sqlite

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import (
    review_process_pool as db_read_review_process_pool,
)
from robert_agent.persistence.queries import sqlite_master as db_read_sqlite_master
from robert_agent.persistence.stores import source_sessions
from robert_agent.persistence.rounds.source_evidence import frozen_prompt_packet

RUN_TABLES = ("review_runs", "vote_runs", "source_model_runs")


def status(conn):
    if not db_read_sqlite_master.object_exists(conn, "review_process_pool"):
        return None
    limit = db_records.first(db_read_review_process_pool.find_for_status(conn))
    if not limit:
        return None
    count = 0
    for table in RUN_TABLES:
        columns = {row[1] for row in db_sqlite.table_info(conn, table)}
        if not columns:
            continue
        if "slot_reserved" not in columns:
            return None
        count += statements.read(
            conn,
            select(func.count()).select_from(tables[table]).where(tables[table].c.slot_reserved == sql_constant(1)),
        )[0][0]
    return {"limit": limit[0], "in_use": count}


def reserve(conn, table, run_id, now):
    if table not in ("review_runs", "vote_runs"):
        raise ValueError("source model slots require the source ownership store")
    return _reserve(conn, table, run_id, now)


def reserve_source(conn, claim, run_id, now):
    """Reserve only for an owned, active source round with a pending request."""
    if not source_sessions.lock_claim(conn, claim, now=now):
        return False
    run, current = (tables[name] for name in ("source_model_runs", "source_rounds"))
    row = db_records.first(
        statements.read(
            conn,
            select(run)
            .select_from(run.join(current, current.c.round_id == run.c.round_id))
            .where(
                run.c.run_id == run_id,
                current.c.source_id == claim.source_id,
                current.c.status == SourceRoundStatus.ACTIVE.value,
            ),
        )
    )
    if (
        row is None
        or frozen_prompt_packet(row) is None
        or not source_sessions.pending_member(conn, row["round_id"], row["request_id"])
    ):
        return False
    return _reserve(conn, "source_model_runs", run_id, now)


def _reserve(conn, table, run_id, now):
    if not conn.in_transaction:
        raise ValueError("model slot reservation requires a known run table and write transaction")
    pool = status(conn)
    if pool is None:
        raise ValueError("model process pool is not initialized")
    if pool["in_use"] >= pool["limit"]:
        return False
    run = tables[table]
    return bool(
        statements.write(
            conn,
            update(run)
            .where(
                run.c.run_id == run_id,
                run.c.status == sql_constant(ModelRunStatus.WAITING.value),
                run.c.slot_reserved == sql_constant(0),
            )
            .values(slot_reserved=1, status=ModelRunStatus.STARTING.value, started_at=now, updated_at=now),
        )
    )
