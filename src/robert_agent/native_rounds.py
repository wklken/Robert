"""Task-scoped access to native model round lifecycles."""
import os

from robert_agent import review_store, vote_store
from robert_agent.model_execution import terminate_children as stop_children

STORES = (review_store, vote_store)


def owner(conn, task_id):
    for store in STORES:
        entry = store.session(conn, task_id=task_id)
        if entry:
            return store, entry
    return None, None


def session(conn, *, task_id):
    return owner(conn, task_id)[1]


def _exit_snapshot(conn, task_id):
    # Read both stores: an owner lookup only returns the first historical session.
    snapshot = []
    for store in STORES:
        entry = store.session(conn, task_id=task_id)
        snapshot.append((entry, store.runs(conn, entry['round_id']) if entry else []))
    return snapshot


def _run_stopped(run):
    from robert_agent.run_once import _dispatch_process_status

    recorded = run['dispatch']
    if not recorded:
        # These states precede reservation, or record explicit abandonment.
        # In particular, an empty starting/running record is never exit proof.
        return not run['slot_reserved'] and run['status'] in {
            'prepared', 'waiting', 'failed', 'interrupted', 'abandoned',
        }
    if not isinstance(recorded, dict):
        return False
    pid = recorded.get('pid')
    if (pid is None and recorded.get('ok') is False
            and recorded.get('status') == 'failed_dispatch'
            and not recorded.get('cleanup_pending')):
        return True  # Dispatcher failed before Popen returned a process.
    if (type(pid) is not int or pid <= 1 or pid == os.getpgrp()
            or recorded.get('process_group_id', pid) != pid):
        return False
    # Reuse complete identity / trusted Popen-exit evidence and descendant
    # observation. Neither a terminal run status nor an absent PID is proof.
    return _dispatch_process_status({'dispatch': recorded})['status'] == 'dead'


def observe_exit(conn, task_id):
    """Read native child exit evidence without signaling or changing the DB.

    Call outside a transaction under task_publication_lock for recovery. A
    board read may use all_stopped as advisory status. Before any mutation,
    revalidate with exit_observation_current in the writer under the same lock.
    Session existence/status is historical evidence, not process liveness.
    """
    if conn.in_transaction:
        raise ValueError('native exit observation requires no active transaction')
    snapshot = _exit_snapshot(conn, task_id)
    stopped = [[run['run_id'] for run in runs if _run_stopped(run)]
               for _, runs in snapshot]
    return {'task_id': task_id, 'snapshot': snapshot, 'stopped_run_ids': stopped,
            'all_stopped': all(len(runs) == len(ids)
                               for (_, runs), ids in zip(snapshot, stopped))}


def exit_observation_current(conn, task_id, observation):
    """Revalidate the full session/run set, including newly registered children."""
    return (observation['task_id'] == task_id
            and _exit_snapshot(conn, task_id) == observation['snapshot'])


def release_observed_slots(conn, task_id, observation):
    """Release only proven-stopped reservations from an unchanged snapshot.

    The caller owns the task lock and writer transaction. Return False when
    any native session/run changed; its parent must retain capacity as well.
    """
    if not conn.in_transaction:
        raise ValueError('native slot release requires a write transaction')
    if not exit_observation_current(conn, task_id, observation):
        return False
    for store, (_, runs), stopped in zip(STORES, observation['snapshot'], observation['stopped_run_ids']):
        for run in runs:
            if run['slot_reserved'] and run['run_id'] in stopped:
                store.release_finished_slot(conn, run)
    return True


def terminate_children(conn, task_id):
    store, entry = owner(conn, task_id)
    return stop_children(conn, task_id, store=store) if entry else 'already_dead'
