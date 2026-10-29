"""Read-only single-protocol cutover guard; never migrate or discard in-flight work."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from robert_agent import result_contract, result_spool


RECOVERY = (
    'Stop new dispatch. Restart the previous version to finish prepared/running workers, '
    'ingest its spool and reconcile all pending publications; verify worker processes stopped. '
    'Alternatively stop and reconcile the affected work, then use Workbench Cancel and '
    'create a replacement task on the new version with the remaining approved scope. '
    'Canceled work items cannot be retried. Never delete DB/spool evidence or run an old prompt on this version.'
)


def inspect(conn, db_path):
    blockers = []
    for attempt, task, status, metadata, lifecycle in conn.execute('''
        SELECT a.attempt_id,a.task_id,a.status,a.metadata_json,t.lifecycle
        FROM attempts a JOIN tasks t ON t.task_id=a.task_id
        WHERE (a.status IN ('running','stale') OR
               (a.status='prepared' AND t.lifecycle IN ('queued','running')))
          AND COALESCE(json_extract(a.metadata_json,'$.result_contract'),'') != ?
        LIMIT 50''', (result_contract.CONTRACT_ID,)):
        if json.loads(metadata or '{}').get('result_contract') == result_contract.CONTRACT_ID:
            continue
        if status in {'running', 'stale'} or (status == 'prepared' and lifecycle in {'queued', 'running'}):
            blockers.append({'kind': 'legacy_' + status, 'task_id': task, 'attempt_id': attempt})
    for result, task in conn.execute("""
        SELECT wr.result_id,wr.task_id FROM worker_results wr
        JOIN tasks t ON t.task_id=wr.task_id
        WHERE COALESCE(json_extract(wr.metadata_json,'$.result_contract'),'') != ?
          AND (t.lifecycle IN ('queued','running') OR EXISTS (
            SELECT 1 FROM workstreams w
            WHERE w.active_task_id=t.task_id AND w.lifecycle='active') OR EXISTS (
            SELECT 1 FROM github_actions ga WHERE ga.result_id=wr.result_id
              AND ga.publish_status='not_published' AND ga.audit_status IN ('pending','accepted')))
        LIMIT 50""", (result_contract.CONTRACT_ID,)):
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
