"""Read and redact durable notification envelopes without changing lifecycle."""
import json

from robert_agent.security.redaction import redact_text
from robert_agent.work import messaging


def _redact_values(value):
    # Redact values before JSON serialization: a path regex applied to encoded
    # JSON can consume closing quotes and corrupt the envelope.
    if isinstance(value, str):
        safe = redact_text(value)
        if not safe['ok']:
            raise ValueError('Message contains sensitive text.')
        return safe['text']
    if isinstance(value, list):
        return [_redact_values(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_values(item) for key, item in value.items()}
    return value


def allowed_repo_ids(conn, config):
    names = (config or {}).get('messaging', {}).get('repos', [])
    configured = {repo['full_name'] for repo in (config or {}).get('repos', [])}
    return frozenset(row[0] for row in conn.execute('SELECT repo_id,full_name FROM repos')
                     if row[1] in names and row[1] in configured)


def envelope(conn, notification_id, *, config=None, include_closed=False):
    row = conn.execute('SELECT task_id,notification_type,status,metadata_json FROM notifications WHERE notification_id=?',
                       (notification_id,)).fetchone()
    if not row:
        return None
    task_id, kind, status, raw = row
    metadata = json.loads(raw or '{}')
    repo_id = messaging.notification_scope(conn, task_id, metadata)
    if config is not None and (repo_id is not None or task_id or metadata.get('work_item_id')):
        if repo_id not in allowed_repo_ids(conn, config):
            return None
    payload = {'protocol': 'robert.message.v1', 'notification_id': notification_id,
               'task_id': task_id, 'type': kind, 'version': 0, 'actions': ['ack'],
               'status': status}
    for key in ('reason_code', 'error_class', 'required_action', 'retryable', 'incident_id'):
        if key in metadata:
            payload[key] = metadata[key]
    payload['safe_error'] = str(metadata.get('safe_error', ''))[:2000]
    if metadata.get('attention_event_id'):
        attention = messaging.attention_details(conn, metadata, include_closed=include_closed)
        if attention is None:
            return None
        payload.update(attention)
        payload['actions'] = messaging.actions_for(attention['event_type'], attention['kind'])
    try:
        return _redact_values(payload)
    except ValueError:
        pass
    # Keep identity, but withhold all free text and mutating actions when the
    # actual decision cannot be safely presented.
    return {'protocol': 'robert.message.v1', 'notification_id': notification_id,
            'task_id': task_id, 'type': kind, 'version': payload['version'],
            'status': status, 'actions': ['ack'],
            'summary': 'Details require local inspection.'}


def pending_filter(conn, config=None):
    """Filter before LIMIT so stale or out-of-scope notices cannot starve delivery."""
    predicate = """ AND (json_extract(metadata_json,'$.attention_event_id') IS NULL OR EXISTS (
        SELECT 1 FROM work_item_events e JOIN work_items wi USING(work_item_id)
        WHERE e.event_id=json_extract(notifications.metadata_json,'$.attention_event_id')
          AND wi.work_item_id=json_extract(notifications.metadata_json,'$.work_item_id')
          AND wi.canceled_at IS NULL AND wi.completed_at IS NULL
          AND NOT EXISTS(SELECT 1 FROM work_item_events r WHERE r.resolves_event_id=e.event_id)))"""
    if not (config or {}).get('messaging', {}).get('enabled'):
        return predicate, []
    allowed = sorted(allowed_repo_ids(conn, config))
    placeholders = ','.join('?' for _ in allowed) or 'NULL'
    predicate += f""" AND (
        (task_id IS NULL AND json_extract(metadata_json,'$.work_item_id') IS NULL
            AND json_extract(metadata_json,'$.repo_id') IS NULL)
        OR COALESCE(
            (SELECT wi.repo_id FROM work_items wi WHERE wi.work_item_id=json_extract(notifications.metadata_json,'$.work_item_id')),
            (SELECT w.repo_id FROM tasks t JOIN workstreams w USING(workstream_id) WHERE t.task_id=notifications.task_id),
            json_extract(metadata_json,'$.repo_id')) IN ({placeholders}))"""
    return predicate, allowed
