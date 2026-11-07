"""One SQLite admission limit shared by native review and voting processes."""

from robert_agent.foundation.contracts.execution import ModelRunStatus
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

RUN_TABLES = ("review_runs", "vote_runs")


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
    if table not in RUN_TABLES or not conn.in_transaction:
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
