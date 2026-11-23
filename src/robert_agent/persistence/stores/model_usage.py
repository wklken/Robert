"""Write one observation per launched model call."""

import json

from sqlalchemy.dialects.sqlite import insert

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables


def record(conn, kind, call_id, scope_id, recorded_at, usage):
    return statements.write(
        conn,
        insert(tables["model_usage"])
        .values(
            kind=kind,
            call_id=call_id,
            scope_id=scope_id,
            recorded_at=recorded_at,
            usage_json=json.dumps(usage, allow_nan=False, sort_keys=True),
        )
        .on_conflict_do_nothing(),
    )
