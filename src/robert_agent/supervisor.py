"""Independent, bounded task observation. Business mutations stay with their owners."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading

from robert_agent import notify, redaction, supervision_config, supervision_observe, supervision_store


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def runtime_dir(config):
    identity = hashlib.sha256(str(Path(config['db_path']).resolve()).encode()).hexdigest()[:16]
    return Path(config['data_dir']) / 'supervision' / identity


def health_path(config):
    return runtime_dir(config) / 'health.json'


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def instance_lock(config):
    directory = runtime_dir(config)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / 'supervisor.lock', os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def connect(db_path, *, readonly=False):
    path = Path(db_path).resolve()
    # mode=rw refuses to create a missing database, just as mode=ro does.
    conn = sqlite3.connect(path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True, timeout=0.1)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _schema_ready(conn):
    if not supervision_store.has_schema(conn):
        raise ValueError('supervision_schema_missing')


def _read_health(config):
    try:
        return json.loads(health_path(config).read_text())
    except (OSError, ValueError):
        return {}


def _health(config, result, now):
    previous = _read_health(config)
    data = {'checked_at': now, 'status': result['status'], 'ok': result['ok'],
            'last_successful_check_at': now if result['ok'] else previous.get('last_successful_check_at'),
            'last_complete_check_at': (now if result.get('coverage_complete') else previous.get('last_complete_check_at')),
            'notification_delivery': 'configured' if os.environ.get('ROBERT_NOTIFICATION_COMMAND') else 'not_configured',
            'error_class': result.get('error_class'), 'pid': os.getpid()}
    if not result['ok']:
        data['first_failed_at'] = previous.get('first_failed_at', now) if not previous.get('ok', True) else now
        data['delivery'] = previous.get('delivery', {}) if not previous.get('ok', True) else {}
        data['delivery_id'] = 'supervisor-health-' + hashlib.sha256(
            (str(Path(config['db_path']).resolve()) + data['first_failed_at']).encode()).hexdigest()[:24]
    atomic_json(health_path(config), data)


def _safe_observation(observation):
    result = dict(observation)
    for field in ('evidence', 'positive_evidence'):
        if field in result:
            safe = redaction.redact_text(json.dumps(result[field], ensure_ascii=False))
            result[field] = json.loads(safe['text']) if safe['ok'] else {'details_omitted': True}
            if field == 'positive_evidence' and not safe['ok']:
                result.pop(field)
    return result


def _notify_changes(conn, changes, now):
    for change in changes:
        if change['transition'] != 'opened':
            continue
        item = change['incident']
        identifier = 'supervision-' + item['incident_id'] + '-opened'
        conn.execute("""INSERT OR IGNORE INTO notifications
            (notification_id, task_id, notification_type, channel, status, created_at, metadata_json)
            VALUES (?,?,'supervision_incident','local','recorded',?,?)""",
            (identifier, item['scope_id'] if item['scope_type'] == 'task' else None, now,
             json.dumps({'reason_code': item.get('reason_code', item['rule_id']),
                         'required_action': 'Inspect the supervision incident and its evidence.',
                         'incident_id': item['incident_id'], 'retryable': False})))


def check(config, *, record=False, now=None):
    now = now or utc_now()
    settings = {**supervision_config.DEFAULTS, **config.get('supervision', {})}
    try:
        with closing(connect(config['db_path'], readonly=True)) as conn:
            _schema_ready(conn)
            state = supervision_store.get_checkpoint(conn, 'service', 'supervisor', 'cursor') if record else None
        cursor = (state or {}).get('metadata', {}).get('cursor', '')
        sampled = supervision_observe.collect(config['db_path'], now=now, limit=settings['batch_size'], config=config, cursor=cursor)
        observations = [_safe_observation(item) for item in sampled['observations']]
        changes = []
        if record:
            with closing(connect(config['db_path'])) as conn, conn:
                conn.execute('BEGIN IMMEDIATE')
                changes = supervision_store.apply_observations(conn, observations, now)
                supervision_store.update_checkpoint(conn, 'service', 'supervisor', 'cursor', now=now,
                                                     metadata={'cursor': sampled.get('cursor', '')})
                _notify_changes(conn, changes, now)
        result = {'ok': True, 'status': 'observed', 'checked_at': now, 'observations': observations,
                  'changes': changes, 'coverage_complete': sampled.get('coverage_complete', False)}
    except (OSError, sqlite3.Error, ValueError) as exc:
        result = {'ok': False, 'status': 'unobservable', 'checked_at': now,
                  'error_class': type(exc).__name__, 'safe_error': 'Observation unavailable; inspect the database and supervision schema.'}
    if record:
        try:
            _health(config, result, now)
        except OSError:
            result['health_persisted'] = False
            print('Robert supervision: observation health cannot be persisted', file=sys.stderr)
    return result


def status(config):
    try:
        with closing(connect(config['db_path'], readonly=True)) as conn:
            _schema_ready(conn)
            incidents = supervision_store.list_incidents(conn, status='active')
        return {'ok': True, 'status': 'ready', 'health': _read_health(config), 'incidents': incidents}
    except (OSError, sqlite3.Error, ValueError):
        return {'ok': False, 'status': 'unobservable', 'health': _read_health(config), 'incidents': []}


def deliver_health(db_path, path, *, runner=subprocess.run):
    """Deliver the one bounded DB-outage notice independently of SQLite."""
    command = os.environ.get('ROBERT_NOTIFICATION_COMMAND')
    if not command:
        return
    state_path = path.with_name('health-delivery.json')
    try:
        health = json.loads(path.read_text())
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
    except (OSError, ValueError):
        return
    if health.get('ok'):
        if state.get('notification_id'):
            try:
                with closing(connect(db_path)) as conn, conn:
                    conn.execute("""INSERT OR IGNORE INTO notifications
                        (notification_id,notification_type,channel,status,created_at,metadata_json)
                        VALUES (?,'supervisor_unobservable','local',?,?,?)""",
                        (state['notification_id'], 'sent' if state.get('status') == 'sent' else 'resolved',
                         state['created_at'], json.dumps({'delivery': state, 'reason_code': 'observation_unavailable'})))
            except (OSError, sqlite3.Error):
                return
        return
    identifier = health.get('delivery_id')
    if not identifier:
        return
    fields = {'reason_code': 'observation_unavailable', 'required_action': 'Inspect the Robert database and supervisor health.', 'retryable': False}
    digest = hashlib.sha256(json.dumps(['supervisor_unobservable', fields], sort_keys=True).encode()).hexdigest()[:16]
    delivery_id = identifier + ':' + digest
    if state.get('delivery_id') != delivery_id:
        state = {'notification_id': identifier, 'delivery_id': delivery_id, 'created_at': health['first_failed_at'], 'attempt_count': 0}
    if state.get('status') == 'sent' or state['attempt_count'] >= 3:
        return
    state.update(status='sending', attempt_count=state['attempt_count'] + 1)
    # Without a durable claim we must not repeatedly send on every failed tick.
    try:
        atomic_json(state_path, state)
    except OSError:
        return
    payload = {'type': 'supervisor_unobservable', 'delivery_id': delivery_id, **fields}
    try:
        sent = runner(shlex.split(command), input=json.dumps(payload), text=True,
                      capture_output=True, timeout=5, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        sent = False
    state.update(status='sent' if sent else 'failed')
    try:
        atomic_json(state_path, state)
    except OSError:
        pass


def run(config, *, once=False, stop=None):
    settings = {**supervision_config.DEFAULTS, **config.get('supervision', {})}
    if settings['mode'] == 'off':
        return {'ok': True, 'status': 'disabled'}
    stop = stop or threading.Event()
    with instance_lock(config) as acquired:
        if not acquired:
            return {'ok': False, 'status': 'already_running'}
        handlers = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                handlers[sig] = signal.signal(sig, lambda *_: stop.set())
        delivery = None
        try:
            while not stop.is_set():
                result = check(config, record=True)
                if delivery is not None and delivery.poll() is not None:
                    delivery.wait()
                    delivery = None
                if delivery is None and os.environ.get('ROBERT_NOTIFICATION_COMMAND'):
                    delivery = subprocess.Popen([sys.executable, '-m', 'robert_agent.supervisor',
                        '--deliver-once', str(config['db_path']), str(health_path(config))], stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                if once:
                    return result
                stop.wait(settings['poll_seconds'])
            return {'ok': True, 'status': 'stopped'}
        finally:
            if delivery is not None:
                try:
                    delivery.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    delivery.terminate()
                    try:
                        delivery.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        delivery.kill()
                        delivery.wait()
            for sig, handler in handlers.items():
                signal.signal(sig, handler)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--deliver-once':
        deliver_health(sys.argv[2], Path(sys.argv[3]))
        try:
            if Path(sys.argv[2]).is_file():
                notify.deliver_pending(sys.argv[2], limit=1)
        except (OSError, sqlite3.Error):
            pass
    else:
        raise SystemExit('Use robert supervisor run/check/status')
