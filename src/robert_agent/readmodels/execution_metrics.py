"""Read-only elapsed-time evidence; missing timestamps remain unknown.

Intervals are independently sampled, not additive: retries and manual waits can
leave uninstrumented gaps. Never infer historical downtime from a queue delay.
"""

from collections import defaultdict
from datetime import datetime, timezone
import json
import math


def _time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _seconds(start, end):
    start, end = _time(start), _time(end)
    return round((end - start).total_seconds(), 3) if start and end and end >= start else None


def _rows(conn, query, args=()):
    cursor = conn.execute(query, args)
    names = [col[0] for col in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def task_timing(conn, task_id):
    tasks = _rows(conn, 'SELECT task_id,task_kind,route_id,lifecycle,created_at,updated_at,metadata_json FROM tasks WHERE task_id=?', (task_id,))
    if not tasks:
        return None
    task = tasks[0]
    attempts = _rows(conn, 'SELECT attempt_id,status,started_at,finished_at,metadata_json FROM attempts WHERE task_id=? ORDER BY attempt_no', (task_id,))
    results = _rows(conn, 'SELECT attempt_id,created_at,metadata_json FROM worker_results WHERE task_id=? ORDER BY created_at', (task_id,))
    for attempt in attempts:
        metadata = json.loads(attempt['metadata_json'] or '{}')
        dispatch = metadata.get('dispatch') or {}
        attempt['worker_started_at'] = metadata.get('worker_started_at') or (
            attempt['started_at'] if attempt['status'] != 'prepared' and dispatch.get('status') == 'running' else None)
    result_by_attempt = {row['attempt_id']: row for row in results}
    worker, ingestion, audit = [], [], []
    for attempt in attempts:
        result = result_by_attempt.get(attempt['attempt_id'], {})
        metadata = json.loads(result.get('metadata_json') or '{}')
        elapsed = _seconds(attempt['worker_started_at'], metadata.get('result_saved_at') or attempt['finished_at'])
        if elapsed is not None:
            worker.append(elapsed)
        elapsed = _seconds(metadata.get('result_saved_at'), metadata.get('result_ingested_at'))
        if elapsed is not None:
            ingestion.append(elapsed)
        elapsed = _seconds(metadata.get('result_ingested_at') or result.get('created_at'), (metadata.get('audit') or {}).get('audited_at'))
        if elapsed is not None:
            audit.append(elapsed)
    actions = _rows(conn, 'SELECT publish_status,metadata_json FROM github_actions WHERE task_id=?', (task_id,))
    publication = [json.loads(row['metadata_json']).get('publish', {}) for row in actions]
    published = [row.get('published_at') for row in publication if _time(row.get('published_at'))]
    next_retry = [row['next_retry_at'] for row in publication if row.get('next_retry_at')]
    wait = json.loads(task['metadata_json'] or '{}').get('wait_state', {})
    reason = 'unknown'
    if task['lifecycle'] in {'completed','failed','canceled','ignored'}:
        reason = 'terminal'
        wait = {}
        next_retry = []
    elif task['lifecycle'] == 'waiting_for_user':
        reason = 'human_input'
    elif any(row.get('retryable') is False and row.get('status') != 'published' for row in publication):
        reason = 'publication_attention'
    elif next_retry:
        reason = 'publication_backoff'
    elif wait.get('reason_code'):
        reason = wait['reason_code']
    elif any(row['publish_status'] == 'not_published' for row in actions):
        reason = 'publication_pending'
    elif task['lifecycle'] == 'queued':
        reason = 'scheduler_pending'
    elif task['lifecycle'] == 'running':
        reason = 'worker_or_result_pending'
    return {
        'task_id': task_id, 'task_kind': task['task_kind'], 'route_id': task['route_id'],
        'lifecycle': task['lifecycle'], 'waiting_reason': reason,
        'waiting_since': wait.get('since'), 'lease_expires_at': wait.get('lease_expires_at'),
        'next_retry_at': min(next_retry) if next_retry else None,
        'queue_seconds': _seconds(task['created_at'], next((row['worker_started_at'] for row in attempts if row['worker_started_at']), None)),
        'worker_seconds': round(sum(worker), 3) if worker else None,
        'result_ingestion_seconds': round(sum(ingestion), 3) if ingestion else None,
        'result_to_audit_seconds': round(sum(audit), 3) if audit else None,
        'result_to_publication_seconds': _seconds(results[-1]['created_at'], max(published, key=_time)) if results and published and all(row['publish_status'] in {'published','skipped'} for row in actions) else None,
        'attempt_count': len(attempts),
    }


def summarize(conn, limit=500):
    """Bound recent tasks and group classification separately from execution."""
    groups = defaultdict(list)
    for (task_id,) in conn.execute('SELECT task_id FROM tasks ORDER BY created_at DESC,task_id DESC LIMIT ?', (limit,)).fetchall():
        timing = task_timing(conn, task_id)
        groups[timing['task_kind'] + ':' + str(timing['route_id'] or 'unknown') + ':' + timing['lifecycle']].append(timing)
    result = {}
    for key, timings in groups.items():
        metrics = {'tasks': len(timings)}
        for metric in ('queue_seconds','worker_seconds','result_ingestion_seconds','result_to_audit_seconds','result_to_publication_seconds'):
            values = sorted(row[metric] for row in timings if row[metric] is not None)
            metrics[metric] = {'samples': len(values), 'missing': len(timings)-len(values),
                               'p50': values[math.ceil(len(values)*.5)-1] if values else None,
                               'p95': values[math.ceil(len(values)*.95)-1] if values else None}
        result[key] = metrics
    return {'sample_limit': limit, 'grouping': 'task_kind:route_id:lifecycle', 'by_task_kind': result,
            'note': 'Intervals can overlap; missing evidence is unknown, not zero. Queue delay does not establish its cause.'}
