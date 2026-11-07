"""work scan_batches responsibilities."""
from robert_agent.work import source_policy as source_policy
from datetime import datetime, timezone
from contextlib import closing
import json
import sqlite3
import time
from uuid import uuid4
from robert_agent.work import wakeup
from robert_agent.adapters.notifications import wakeup as wakeup_notifier


MAX_BATCH_RETRIES = 5


def save_reads(conn, repo_id, responses, when):
    for key, payload in dict(responses).items():
        conn.execute('''INSERT INTO github_scan_reads(repo_id,command_key,response_json,observed_at)
            VALUES (?,?,?,?) ON CONFLICT(repo_id,command_key) DO UPDATE SET
            response_json=excluded.response_json,observed_at=excluded.observed_at''',
            (repo_id, key, payload, when))



def save_batch(db_path, repo_id, context, raw_events, responses, *, now=None, complete_sources=()):
    when = time.time() if now is None else now
    identifier = uuid4().hex
    with closing(sqlite3.connect(db_path, timeout=5)) as conn, conn:
        previous = conn.execute('SELECT batch_id,context_digest,raw_events_json,attention_reason FROM github_scan_batches WHERE repo_id=? AND context_digest=? AND raw_events_json=? AND completed_at IS NULL ORDER BY created_at DESC LIMIT 1', (repo_id, context, json.dumps(raw_events))).fetchone()
        if previous and previous[1] == context and previous[2] == json.dumps(raw_events):
            identifier = previous[0]
            conn.execute('UPDATE github_scan_batches SET complete_sources_json=? WHERE batch_id=?', (json.dumps(list(complete_sources)), identifier))
        conn.execute('''INSERT OR IGNORE INTO github_scan_batches
            (batch_id,repo_id,context_digest,raw_events_json,created_at,retry_at,complete_sources_json)
            VALUES (?,?,?,?,?,?,?)''',
            (identifier, repo_id, context, json.dumps(raw_events), when, when, json.dumps(list(complete_sources))))
        save_reads(conn, repo_id, responses, when)
        if not (previous and previous[3]) and conn.execute('SELECT 1 FROM repos WHERE repo_id=?', (repo_id,)).fetchone():
            wakeup.request_wakeup(conn, repo_id=repo_id, reason='manual_operator_request',
                                  dedupe_key=identifier, metadata={'kind': 'github_observation_ready'})
    wakeup_notifier.notify(db_path)
    return identifier



def next_batch(db_path, repo_id, *, now=None, context=None):
    with closing(sqlite3.connect(db_path, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        clause = ' AND context_digest=?' if context is not None else ''
        params = [repo_id, time.time() if now is None else now] + ([context] if context is not None else [])
        row = conn.execute('SELECT * FROM github_scan_batches WHERE repo_id=? AND completed_at IS NULL AND attention_reason IS NULL AND retry_at<=?' + clause + ' ORDER BY created_at,batch_id LIMIT 1', params).fetchone()
    if not row:
        return None
    # A newer failed read stores an explicit unknown, never an older success.
    with closing(sqlite3.connect(db_path, timeout=5)) as conn:
        reads = conn.execute('SELECT command_key,response_json FROM github_scan_reads WHERE repo_id=? AND observed_at>=?',
                             (repo_id, (time.time() if now is None else now) - 3600)).fetchall()
    return {**dict(row), 'raw_events': json.loads(row['raw_events_json']),
            'responses': dict(reads)}



def has_pending(conn, repo_ids, *, now=None, contexts=None):
    when = time.time() if now is None else now
    for repo_id in repo_ids:
        if contexts is not None and repo_id not in contexts:
            continue
        clause = ' AND context_digest=?' if contexts is not None else ''
        params = [repo_id, when] + ([contexts[repo_id]] if contexts is not None else [])
        if conn.execute('''SELECT 1 FROM github_scan_batches WHERE repo_id=?
                AND completed_at IS NULL AND attention_reason IS NULL AND retry_at<=?''' + clause + ' LIMIT 1', params).fetchone():
            return True
    return False



def fail_batch(conn, batch_id, reason, when):
    row = conn.execute('SELECT repo_id,retry_count,attention_reason FROM github_scan_batches WHERE batch_id=? AND completed_at IS NULL', (batch_id,)).fetchone()
    if not row or row[2]:
        return
    count = row[1] + 1
    attention = reason if count >= MAX_BATCH_RETRIES else None
    retry_at = when + min(300, 30 * (2 ** (count - 1)))
    conn.execute('UPDATE github_scan_batches SET retry_count=?,attention_reason=?,retry_at=? WHERE batch_id=?', (count, attention, retry_at, batch_id))
    conn.execute("UPDATE wakeups SET status=?,not_before_at=?,updated_at=? WHERE json_extract(metadata_json,'$.kind')='github_observation_ready' AND dedupe_key=?",
                 ('canceled' if attention else 'pending', datetime.fromtimestamp(retry_at, timezone.utc).isoformat(), datetime.fromtimestamp(when, timezone.utc).isoformat(), batch_id))
    if attention:
        conn.execute('''INSERT INTO notifications(notification_id,notification_type,channel,status,created_at,metadata_json)
            VALUES (?,'github_scan_attention','local','recorded',?,?)
            ON CONFLICT(notification_id) DO UPDATE SET status=excluded.status,
                created_at=excluded.created_at,metadata_json=excluded.metadata_json''',
            ('github-scan:' + batch_id, datetime.fromtimestamp(when, timezone.utc).isoformat(),
             json.dumps({'repo_id': row[0], 'batch_id': batch_id, 'reason_code': reason, 'retry_count': count,
                         'required_action': f'Inspect GitHub batch {batch_id} (attention at {when}); restore access and explicitly retry.'})))



def retry_batch(db_path, batch_id, repo_ids, *, now=None):
    when = time.time() if now is None else now
    with closing(sqlite3.connect(db_path, timeout=5)) as conn, conn:
        row = conn.execute('SELECT repo_id,attention_reason FROM github_scan_batches WHERE batch_id=? AND completed_at IS NULL', (batch_id,)).fetchone()
        if not row or row[0] not in repo_ids or not row[1]:
            return {'ok': False, 'status': 'not_in_scope_or_not_attention'}
        conn.execute('UPDATE github_scan_batches SET retry_count=0,attention_reason=NULL,retry_at=? WHERE batch_id=?', (when, batch_id))
        conn.execute("UPDATE wakeups SET status='pending',not_before_at=? WHERE json_extract(metadata_json,'$.kind')='github_observation_ready' AND dedupe_key=?", (datetime.fromtimestamp(when, timezone.utc).isoformat(), batch_id))
        conn.execute("UPDATE notifications SET status='resolved' WHERE notification_id=?", ('github-scan:' + batch_id,))
    wakeup_notifier.notify(db_path)
    return {'ok': True, 'status': 'retry_requested', 'batch_id': batch_id}



def finish_batch(db_path, batch_id, *, success, now=None, failure_reason="local_processing_failed"):
    when = time.time() if now is None else now
    with closing(sqlite3.connect(db_path, timeout=5)) as conn, conn:
        if success:
            current = conn.execute('SELECT repo_id,context_digest,created_at,complete_sources_json,raw_events_json FROM github_scan_batches WHERE batch_id=?', (batch_id,)).fetchone()
            if current:
                covered = {tuple(source) for source in json.loads(current[3])}
                replayed = {source_policy.event_identity(raw) for raw in json.loads(current[4])}
                older = conn.execute('SELECT batch_id,raw_events_json FROM github_scan_batches WHERE repo_id=? AND context_digest!=? AND created_at<=? AND completed_at IS NULL', (current[0], current[1], current[2])).fetchall()
                for old_id, encoded in older:
                    sources = {(event.get('source_type', 'issue'), event['number']) for event in json.loads(encoded)}
                    if sources <= covered and {source_policy.event_identity(raw) for raw in json.loads(encoded)} <= replayed:
                        conn.execute('UPDATE github_scan_batches SET completed_at=? WHERE batch_id=?', (when, old_id))
                        conn.execute("UPDATE wakeups SET status='consumed' WHERE json_extract(metadata_json,'$.kind')='github_observation_ready' AND dedupe_key=?", (old_id,))
            conn.execute('UPDATE github_scan_batches SET completed_at=? WHERE batch_id=?', (when, batch_id))
            conn.execute("UPDATE wakeups SET status='consumed' WHERE json_extract(metadata_json,'$.kind')='github_observation_ready' AND dedupe_key=?", (batch_id,))
        else:
            fail_batch(conn, batch_id, failure_reason, when)
        conn.execute("DELETE FROM wakeups WHERE status='consumed' AND json_extract(metadata_json,'$.kind') IN ('github_observation_ready','github_context_refresh')")
        conn.execute('DELETE FROM github_scan_batches WHERE completed_at IS NOT NULL')
        conn.execute('DELETE FROM github_scan_reads WHERE observed_at < ?', (when - 3600,))
