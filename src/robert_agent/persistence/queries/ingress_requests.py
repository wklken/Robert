"""Materialized snapshots of individual ingress requests."""

from sqlalchemy import select

from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables


def find_by_id(conn, request_id):
    request = tables["ingress_requests"]
    return records.first(statements.read(conn, select(request).where(request.c.request_id == request_id)))
