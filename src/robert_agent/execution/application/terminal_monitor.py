"""Finish exited attempts whose workflow has already ended execution authority."""

from robert_agent.adapters.processes import attempt as attempt_process
from robert_agent.delivery.lock import task_publication_lock
from robert_agent.execution import ownership, rounds, spool
from robert_agent.execution.usage import record_dispatch_usage
from robert_agent.foundation import values
from robert_agent.foundation.contracts.execution import AttemptStatus
from robert_agent.persistence import records, transactions
from robert_agent.persistence.dialects import sqlite
from robert_agent.persistence.queries import attempts, worker_results
from robert_agent.persistence.stores import attempts as write_attempts
from robert_agent.persistence.stores import tasks as write_tasks
from robert_agent.persistence.stores import workstreams as write_workstreams
import json


def finish(conn, attempt_id, task_id, workstream_id, snapshot, binding, now, data_dir, *, task_lock_held=False):
    """Release proven-exited execution, retaining its terminal workflow fence."""
    conn.commit()
    database = sqlite.main_database_path(conn)
    if not database:
        return False
    if task_lock_held:
        return _finish_locked(conn, attempt_id, task_id, workstream_id, snapshot, binding, now, data_dir, database)
    with task_publication_lock(database, task_id) as acquired:
        return acquired and _finish_locked(
            conn, attempt_id, task_id, workstream_id, snapshot, binding, now, data_dir, database
        )


def _finish_locked(conn, attempt_id, task_id, workstream_id, snapshot, binding, now, data_dir, database):
    metadata = values.json_object(snapshot[2])
    if attempt_process.dispatch_status(metadata)["status"] != "dead":
        return False
    children = rounds.observe_exit(conn, task_id)
    if not children["all_stopped"]:
        return False
    try:
        with spool.locked_record(database, attempt_id, blocking=False) as path:
            if path.exists():
                return False  # Saved results require their own reconciliation.
            transactions.begin(conn, immediate=True)
            fresh = records.first(attempts.find_for_supervision_observation_current(conn, attempt_id, snapshot[0]))
            if (
                not fresh
                or tuple(fresh) != snapshot
                or ownership.terminal_subject_for_task(conn, task_id) != binding
                or records.first(worker_results.find_result_id_by_attempt_id(conn, attempt_id))
                or not rounds.exit_observation_current(conn, task_id, children)
            ):
                conn.rollback()
                return False
            if not rounds.release_observed_slots(conn, task_id, children):
                conn.rollback()
                return False
            failure = {
                "status": "execution_authority_ended",
                "subject_type": binding["subject_type"],
                "subject_id": binding["subject_id"],
                "terminal_at": binding["terminal_at"],
            }
            metadata.update(process_stopped_at=now, supervise=failure)
            if data_dir is not None:
                record_dispatch_usage(
                    conn, data_dir, "worker", attempt_id, task_id, metadata.get("dispatch"), terminal=True
                )
            write_attempts.record_dispatch_result_2(
                conn, AttemptStatus.FAILED.value, now, json.dumps(failure), json.dumps(metadata), attempt_id
            )
            write_tasks.update_for_supervise_running_attempts(conn, now, task_id)
            write_workstreams.finish_ended_task(conn, workstream_id, task_id, now)
            conn.commit()  # Keep the final-result lock through the terminal transition.
            return True
    except BlockingIOError:
        return False
    except BaseException:
        conn.rollback()
        raise
