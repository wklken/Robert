"""Materialized observations for one durable model call."""

from sqlalchemy import select

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.records import first


def get(conn, kind, call_id):
    usage = tables["model_usage"]
    return first(statements.read(conn, select(usage).where(usage.c.kind == kind, usage.c.call_id == call_id)))
