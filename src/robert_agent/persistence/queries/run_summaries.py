"""Resolve shared diagnostic summaries in the same statement as their owner."""

from sqlalchemy import case, func, select

from robert_agent.persistence.catalog import tables
from robert_agent.persistence.expressions import sql_constant, valid_json_or_empty
from robert_agent.persistence.queries import sqlite_master


def shared_schema_available(conn):
    return sqlite_master.table_exists(conn, "agent_run_summary_refs") and sqlite_master.table_exists(
        conn, "diagnostic_run_summaries"
    )


def resolved_summary(conn, runs):
    if not shared_schema_available(conn):
        return runs.c.summary_json
    refs, summaries = tables["agent_run_summary_refs"], tables["diagnostic_run_summaries"]
    shared = (
        select(summaries.c.summary_json)
        .select_from(refs.join(summaries, refs.c.summary_id == summaries.c.summary_id))
        .where(refs.c.run_id == runs.c.run_id)
        .correlate(runs)
        .scalar_subquery()
    )
    marker = func.json_extract(valid_json_or_empty(runs.c.summary_json), sql_constant("$.summary_ref"))
    return case(
        (marker == "diagnostic_run_summaries", func.coalesce(shared, runs.c.summary_json)),
        else_=runs.c.summary_json,
    )
