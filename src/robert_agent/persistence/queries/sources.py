"""Materialized source lookups by their provider-scoped external identity."""

from sqlalchemy import select

from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables


def find_by_identity(conn, *, provider, host, project_key, source_type, external_id):
    source = tables["sources"]
    return records.first(
        statements.read(
            conn,
            select(source).where(
                source.c.provider == provider,
                source.c.host == host,
                source.c.project_key == project_key,
                source.c.source_type == source_type,
                source.c.external_id == external_id,
            ),
        )
    )
