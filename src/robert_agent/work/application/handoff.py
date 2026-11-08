"""Durable unfinished event handoffs after an accepted result is completed."""

from robert_agent.foundation.contracts.supervision import RecoveryActionKind

from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.delivery import contract as result_contract


def ready_rows(conn, repo_ids, now, *, config=None):
    if not repo_ids:
        return []
    rows = db_read_tasks.ready_rows_filtered(conn, repo_ids, result_contract.CONTRACT_ID, now)
    from robert_agent.supervision import actions as supervision_actions

    return [
        (task_id, workstream_id)
        for task_id, workstream_id, retries in rows
        if not supervision_actions.blocks_consumer(
            conn,
            task_id=task_id,
            kind=RecoveryActionKind.HANDOFF_RESUME.value,
            target_id=task_id,
            now=now,
            config=config,
            native_retry=bool(retries),
        )
    ]
