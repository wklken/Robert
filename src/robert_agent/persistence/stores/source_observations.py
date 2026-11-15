"""Record discovery checkpoints and hold source authorization under a writer."""

import json

from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert

from robert_agent.foundation.contracts.work import SourceIngressPhase
from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables


def ensure_candidate(conn, *, source_id, host, workspace_id, kind, external_id):
    source = tables["sources"]
    statements.write(
        conn,
        insert(source)
        .values(
            source_id=source_id,
            source_key=source_id,
            provider="tapd",
            host=host,
            project_key=workspace_id,
            source_type=kind,
            external_id=external_id,
            state="unknown",
            metadata_json="{}",
        )
        .on_conflict_do_nothing(
            index_elements=[
                source.c.provider,
                source.c.host,
                source.c.project_key,
                source.c.source_type,
                source.c.external_id,
            ]
        ),
    )


def _progress(conn, table, key_name, key, now, *, completed, reason):
    stmt = insert(table).values(
        {key_name: key, "attempted_at": now, "completed_at": now if completed else None, "attention_reason": reason}
    )
    key_column = table.c[key_name]
    return statements.write(
        conn,
        stmt.on_conflict_do_update(
            index_elements=[key_column],
            set_={
                "attempted_at": now,
                "completed_at": now if completed else table.c.completed_at,
                "attention_reason": reason,
            },
            where=table.c.attempted_at <= now,
        ),
    )


def discovery_checkpoint(conn, key, now, *, completed=False, reason=None):
    return _progress(conn, tables["tapd_discovery_progress"], "probe_key", key, now, completed=completed, reason=reason)


def observation_checkpoint(conn, source_id, now, *, completed=False, reason=None):
    return _progress(
        conn, tables["tapd_observation_progress"], "source_id", source_id, now, completed=completed, reason=reason
    )


def hold_source(conn, source_id, metadata, reason, now, *, pause=False):
    source, request = tables["sources"], tables["ingress_requests"]
    updated = {**metadata, "coverage": "incomplete", "reason": reason}
    # Preserve the last provider observation, its body, and all source identities.
    # Invalidating its revision prevents readers from authorizing through a stale cache.
    updated["revision"] = None
    if pause and (binding := updated.get("binding")) and binding.get("state") == "active":
        updated["binding"] = {**binding, "state": "paused", "reason": reason}
    statements.write(
        conn,
        update(source)
        .where(source.c.source_id == source_id, source.c.provider == "tapd")
        .values(metadata_json=json.dumps(updated, ensure_ascii=False, sort_keys=True)),
    )
    phases = (
        [SourceIngressPhase.PENDING.value, SourceIngressPhase.HELD.value]
        if pause
        else [SourceIngressPhase.PENDING.value]
    )
    statements.write(
        conn,
        update(request)
        .where(
            request.c.source_id == source_id,
            request.c.provider == "tapd",
            request.c.phase.in_(phases),
        )
        .values(phase=SourceIngressPhase.PAUSED.value if pause else SourceIngressPhase.HELD.value, updated_at=now),
    )
