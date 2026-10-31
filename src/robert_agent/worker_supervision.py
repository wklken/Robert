"""Bounded supervision of existing workers while automatic admission is paused."""
import argparse
from contextlib import ExitStack, closing
import json
import os
import sqlite3

from robert_agent import result_spool, run_once, validate_config, worker_identity
from robert_agent.worker.result import replay_pending_results
from robert_agent.common import ControlDeadlineExceeded, emit
from robert_agent.publication_lock import task_publication_lock


def run(config_path):
    config = validate_config.validate_config(config_path, skip_external=True)
    if not config.get('ok'):
        return config
    db_path = config['db_path']
    replay_pending_results(db_path)
    now = run_once._now()
    run_id = run_once._id('supervision')
    summary = {'executor_id': os.environ.get('_ROBERT_EXECUTOR_ID'),
               'executor_identity': worker_identity.capture_process_identity(os.getpid()),
               'supervision_only': True}
    supervised = 0
    failures = []
    with closing(sqlite3.connect(db_path, timeout=5)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        with conn:
            conn.execute("INSERT INTO agent_runs(run_id,status,started_at,config_path,summary_json) VALUES(?,'running',?,?,?)",
                         (run_id, now, str(config_path), json.dumps(summary)))
        try:
            for repo in config['repos']:
                repo_id = run_once._repo_id(repo['full_name'])
                if not conn.execute('SELECT 1 FROM repos WHERE repo_id=?', (repo_id,)).fetchone():
                    continue
                with conn:
                    lease = run_once._acquire_agent_lease(conn, repo_id, run_id, now, config.get('lease_ttl_minutes', 9))
                if not lease['ok']:
                    continue
                try:
                    # Same task locks as cancellation, result isolation and dispatch.
                    task_ids = conn.execute("SELECT task_id FROM tasks WHERE workstream_id IN (SELECT workstream_id FROM workstreams WHERE repo_id=?) AND lifecycle IN ('queued','running')", (repo_id,)).fetchall()
                    for (task_id,) in task_ids:
                        with task_publication_lock(db_path, task_id) as acquired:
                            if not acquired:
                                continue
                            try:
                                # Submission owns this lock from pre-audit through durable spool/DB
                                # handoff. Hold it through finalization so a deferred or concurrent
                                # result cannot be turned into a failed execution before ingestion.
                                with ExitStack() as result_locks:
                                    attempts = conn.execute('SELECT attempt_id FROM attempts WHERE task_id=?', (task_id,)).fetchall()
                                    paths = [result_locks.enter_context(result_spool.locked_record(db_path, attempt_id, blocking=False))
                                             for (attempt_id,) in attempts]
                                    if any(path.exists() for path in paths):
                                        continue
                                    supervised += run_once._supervise_running_attempts(conn, repo_id, config,
                                        run_once._now(), allow_recovery=False, task_id=task_id)
                                    conn.commit()
                            except BlockingIOError:
                                continue  # A submit/replay still owns durable result handoff.
                            except ControlDeadlineExceeded:
                                raise
                            except Exception as exc:
                                conn.rollback()
                                failures.append({'repo_id': repo_id, 'task_id': task_id, 'error_type': type(exc).__name__})
                except ControlDeadlineExceeded:
                    conn.rollback()
                    failures.append({'repo_id': repo_id, 'error_type': 'ControlDeadlineExceeded'})
                    raise
                except Exception as exc:
                    conn.rollback()
                    failures.append({'repo_id': repo_id, 'error_type': type(exc).__name__})
                finally:
                    with conn:
                        run_once._release_agent_lease(conn, lease['lease_id'], run_once._now())
        finally:
            with conn:
                summary.update(supervised_count=supervised, failures=failures)
                conn.execute('UPDATE agent_runs SET status=?,finished_at=?,summary_json=? WHERE run_id=?',
                    ('failed' if failures else 'completed', run_once._now(), json.dumps(summary), run_id))
    return {'ok': not failures, 'status': 'supervised' if not failures else 'supervision_failed',
            'run_id': run_id, 'supervised_count': supervised, 'failures': failures}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    result = run(args.config)
    return emit(result, 0 if result['ok'] else 3)


if __name__ == '__main__':
    raise SystemExit(main())
