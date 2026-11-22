"""Read model worker runs belonging to a concrete task attempt."""

from sqlalchemy import select

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables


def model_runs_for_attempt(conn, task_id, attempt_id, *, limit=100):
    result = []
    for prefix in ("review", "vote", "pre_pr_review"):
        run = tables[prefix + "_runs"]
        session = tables[prefix + "_sessions"]
        query = (
            select(run)
            .select_from(run.join(session, run.c.round_id == session.c.round_id))
            .where(session.c.task_id == task_id, run.c.owner_attempt_id == attempt_id)
            .order_by(run.c.created_at.desc(), run.c.run_id.desc())
            .limit(limit)
        )
        result.extend(statements.read(conn, query, {}))
    return result
