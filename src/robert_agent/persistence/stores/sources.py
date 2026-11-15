"""Source writes within the caller's transaction."""

import json

from sqlalchemy.dialects.sqlite import insert

from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables

_IDENTITY_FIELDS = ("provider", "host", "project_key", "source_type", "external_id")


def upsert_tapd_source(
    conn,
    *,
    source_id,
    source_key,
    host,
    project_key,
    source_type,
    external_id,
    html_url,
    title,
    state,
    author_login=None,
    source_updated_at=None,
    metadata=None,
):
    """Persist a TAPD observation and return the canonical materialized record."""
    source = tables["sources"]
    identity = {
        "provider": "tapd",
        "host": host,
        "project_key": project_key,
        "source_type": source_type,
        "external_id": external_id,
    }
    if any(not isinstance(value, str) or not value.strip() for value in identity.values()):
        raise ValueError("Source identity coordinates must be non-empty strings")
    if source_type not in {"story", "bug"}:
        raise ValueError("TAPD source type must be story or bug")
    if any(not isinstance(value, str) or not value.strip() for value in (source_id, source_key)):
        raise ValueError("Source ID and key must be non-empty strings")
    observation = {
        "html_url": html_url,
        "title": title,
        "state": state,
        "author_login": author_login,
        "source_updated_at": source_updated_at,
        "metadata_json": json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
    }
    stmt = insert(source).values(source_id=source_id, source_key=source_key, **identity, **observation)
    query = stmt.on_conflict_do_update(
        index_elements=list(_IDENTITY_FIELDS),
        set_={field: stmt.excluded[field] for field in observation},
        where=(source.c.source_id == source_id) & (source.c.source_key == source_key),
    ).returning(*source.c)
    row = records.first(statements.read(conn, query))
    if row is None:
        raise ValueError("TAPD source coordinates conflict with an existing ID or key")
    return row
