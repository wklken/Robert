"""Save the latest exact current PR decision under the caller's source fence."""

import json

from sqlalchemy.dialects.sqlite import insert

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.stores import source_sessions


def save(conn, claim, *, round_id, repo_id, decision, now):
    if source_sessions.lock_round(conn, claim, round_id, now=now) is None:
        return False
    selection = tables["source_pr_selections"]
    fields = {
        "observed_at": now,
        "decision_json": json.dumps(decision, ensure_ascii=False, sort_keys=True, allow_nan=False),
    }
    stmt = insert(selection).values(round_id=round_id, repo_id=repo_id, **fields)
    return bool(
        statements.write(
            conn,
            stmt.on_conflict_do_update(
                index_elements=[selection.c.round_id, selection.c.repo_id],
                set_=fields,
            ),
        )
    )
