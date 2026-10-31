"""Single-protocol guard and isolation of incompatible task evidence."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

from robert_agent import result_contract, result_spool


RECOVERY = (
    'Inspect the isolated execution and confirm its workers stopped. Reconcile pending publications. '
    'For GitHub tasks, use Retrigger to create a replacement with refreshed context. '
    'Alternatively restart the previous version to finish prepared/running workers, '
    'ingest its spool and reconcile all pending publications; verify worker processes stopped. '
    'Alternatively stop and reconcile the affected work, then use Workbench Cancel and '
    'create a replacement task on the new version with the remaining approved scope. '
    'Canceled work items cannot be retried. Never delete DB/spool evidence or run an old prompt on this version.'
)


def inspect(conn, db_path):
    blockers = []
    columns = {row[1] for row in conn.execute('PRAGMA table_info(tasks)')}
    isolation = (" AND COALESCE(json_extract(t.metadata_json,'$.execution_attention.reason_code'),'') != 'result_contract_cutover'"
                 if 'metadata_json' in columns else '')
    for attempt, task, status, metadata, lifecycle in conn.execute(f'''
        SELECT a.attempt_id,a.task_id,a.status,a.metadata_json,t.lifecycle
        FROM attempts a JOIN tasks t ON t.task_id=a.task_id
        WHERE (a.status IN ('running','stale') OR
               (a.status='prepared' AND t.lifecycle IN ('queued','running')))
          AND COALESCE(json_extract(a.metadata_json,'$.result_contract'),'') != ?
          {isolation}
        ''', (result_contract.CONTRACT_ID,)):
        if json.loads(metadata or '{}').get('result_contract') == result_contract.CONTRACT_ID:
            continue
        if status in {'running', 'stale'} or (status == 'prepared' and lifecycle in {'queued', 'running'}):
            blockers.append({'kind': 'legacy_' + status, 'task_id': task, 'attempt_id': attempt})
    # Build unresolved ID sets once. Correlated EXISTS would rescan all historical
    # workstreams/actions for every legacy result on databases without matching indexes.
    for result, task in conn.execute(f"""
        SELECT wr.result_id,wr.task_id FROM worker_results wr
        JOIN tasks t ON t.task_id=wr.task_id
        WHERE COALESCE(json_extract(wr.metadata_json,'$.result_contract'),'') != ?
          AND (t.lifecycle IN ('queued','running') OR t.task_id IN (
            SELECT w.active_task_id FROM workstreams w
            WHERE w.lifecycle='active') OR wr.result_id IN (
            SELECT ga.result_id FROM github_actions ga
            WHERE ga.publish_status='not_published' AND ga.audit_status IN ('pending','accepted')))
          {isolation}
        """, (result_contract.CONTRACT_ID,)):
        blockers.append({'kind': 'legacy_unresolved_result', 'task_id': task, 'result_id': result})
    root = result_spool.directory(db_path)
    for path in sorted(root.glob('*.json')):
        try:
            record = json.loads(path.read_text())
            result_spool._validate_record(record)
        except FileNotFoundError:
            continue  # Concurrent ingestion already removed this pending record.
        except (ValueError, OSError, TypeError, KeyError):
            blockers.append({'kind': 'legacy_or_invalid_spool', 'path': str(path)})
    # Rejected old formats are preserved under this extension by the replay reader.
    blockers.extend({'kind': 'unreconciled_invalid_spool', 'path': str(path)} for path in sorted(root.glob('*.invalid')))
    # Quarantined tasks cannot run, consume results or publish. Their evidence
    # remains intact, but it must not prevent unrelated tasks from proceeding.
    return {'ok': not blockers, 'status': 'ready' if not blockers else 'blocked_result_contract_cutover',
            'result_contract': result_contract.CONTRACT_ID, 'blockers': blockers,
            **({'required_action': RECOVERY} if blockers else {})}


def check(db_path):
    path = Path(db_path)
    if not path.exists():
        return {'ok': True, 'status': 'ready', 'blockers': []}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
            return inspect(conn, path)
    except (sqlite3.Error, ValueError, OSError, TypeError):
        return {'ok': False, 'status': 'blocked_result_contract_cutover',
                'safe_error': 'Cannot inspect result contract cutover state.', 'required_action': RECOVERY}


def prepare(db_path, *, now=None, isolated_only=False):
    try:
        return _prepare(db_path, now=now, isolated_only=isolated_only)
    except (sqlite3.Error, ValueError, OSError, TypeError):
        return {'ok': False, 'status': 'blocked_result_contract_cutover',
                'safe_error': 'Cannot safely isolate incompatible execution state.', 'required_action': RECOVERY}


def _prepare(db_path, *, now=None, isolated_only=False):
    """Isolate identified task blockers; unknown spool/read failures stay global.

    This never converts an old result or attests that a live worker stopped.
    Cancellation and replacement remain explicit operator actions.
    """
    from robert_agent import execution_store, native_rounds, run_once, supervise, work_items
    from robert_agent.publication_lock import task_publication_lock

    now = now or datetime.now(timezone.utc).isoformat()
    checked = {'blockers': []} if isolated_only else check(db_path)
    if not Path(db_path).exists():
        return {'ok': True, 'status': 'ready', 'blockers': []}
    task_ids = {item['task_id'] for item in checked.get('blockers', []) if item.get('task_id')}
    # Isolation prevents admission and result consumption, but exit observation
    # must continue until every recorded process has released its capacity.
    with closing(sqlite3.connect(db_path, timeout=5)) as reader:
        task_ids.update(row[0] for row in reader.execute(f"""SELECT DISTINCT a.task_id
            FROM attempts a JOIN tasks t ON t.task_id=a.task_id
            WHERE json_extract(t.metadata_json,'$.execution_attention.reason_code')='result_contract_cutover'
              AND {supervise.occupied_attempt_sql()}"""))
    for task_id in sorted(task_ids):
        with task_publication_lock(db_path, task_id) as acquired:
            if not acquired:
                continue
            with closing(sqlite3.connect(db_path, timeout=5)) as conn:
                conn.row_factory = sqlite3.Row
                snapshots = list(conn.execute('SELECT a.* FROM attempts a WHERE task_id=? AND ' + supervise.occupied_attempt_sql(), (task_id,)))
                stopped = []
                # Observe processes outside a writer transaction. Retain occupied
                # capacity whenever identity or descendant cleanup is uncertain.
                for attempt in snapshots:
                    metadata = json.loads(attempt['metadata_json'] or '{}')
                    if run_once._dispatch_process_status(metadata)['status'] == 'dead':
                        _, owner = native_rounds.owner(conn, task_id)
                        if not owner:
                            stopped.append(attempt)
                with conn:
                    conn.execute('BEGIN IMMEDIATE')
                    task = conn.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
                    if not task:
                        continue
                    isolated = json.loads(task['metadata_json'] or '{}').get('execution_attention', {}).get('reason_code') == 'result_contract_cutover'
                    if not isolated:
                        if task['lifecycle'] in {'canceled', 'ignored'} or isolated_only:
                            continue
                        if not any(item.get('task_id') == task_id for item in inspect(conn, db_path)['blockers']):
                            continue
                    attention = {'reason_code': 'result_contract_cutover', 'retryable': False,
                                 'required_action': RECOVERY}
                    if not isolated:
                        conn.execute("UPDATE tasks SET metadata_json=json_set(metadata_json,'$.execution_attention',json(?)),updated_at=? WHERE task_id=?",
                                     (json.dumps(attention), now, task_id))
                        if task['task_kind'] == 'human_request' and task['lifecycle'] not in {'completed', 'failed'}:
                            execution_store.ensure_subject(conn, 'task', task_id, 'human_request', now)
                            execution_store.mark_binding_attention(conn, 'task', task_id, now)
                    _, owner = native_rounds.owner(conn, task_id)
                    if owner:
                        stopped = []
                    for attempt in stopped:
                        # A completed result remains historical evidence. Only
                        # mark its process stopped; do not rewrite its outcome.
                        if attempt['status'] == 'completed':
                            conn.execute("""UPDATE attempts SET metadata_json=json_set(metadata_json,'$.process_stopped_at',?)
                                WHERE attempt_id=? AND status=? AND metadata_json=?""",
                                (now, attempt['attempt_id'], attempt['status'], attempt['metadata_json']))
                            continue
                        conn.execute("""UPDATE attempts SET status='failed',finished_at=?,failure_json=?,
                            metadata_json=json_set(metadata_json,'$.process_stopped_at',?)
                            WHERE attempt_id=? AND status=? AND metadata_json=?""",
                            (now, json.dumps(attention), now, attempt['attempt_id'], attempt['status'], attempt['metadata_json']))
                    if isolated:
                        continue  # Existing attention remains until explicit recovery.
                    conn.execute("""INSERT OR IGNORE INTO notifications
                        (notification_id,task_id,notification_type,channel,status,created_at,metadata_json)
                        VALUES (?,?,'execution_attention_required','local','recorded',?,?)""",
                        ('notification-cutover-' + task_id, task_id, now, json.dumps(attention)))
                    item = work_items.resolve_work_item_for_workstream(conn, task['workstream_id'])
                    if item:
                        work_items.record_system_event(conn, item['work_item_id'], event_type='operator_decision_required',
                            idempotency_key='result-contract-cutover:' + task_id,
                            body='Automatic execution stopped: incompatible worker result. ' + RECOVERY,
                            metadata={'kind': 'result_contract_cutover', 'task_id': task_id}, now=now)
    return {'ok': True, 'status': 'isolated_processes_observed'} if isolated_only else check(db_path)
