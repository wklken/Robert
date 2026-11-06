"""Independent, bounded task observation. Business mutations stay with their owners."""

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
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
import time

from robert_agent import notify, redaction, supervision_actions, supervision_diagnose, supervision_observe, supervision_store
from robert_agent.configuration import supervision as supervision_config


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
        continuing = not previous.get('ok', True)
        data['consecutive_failures'] = previous.get('consecutive_failures', 1) + 1 if continuing else 1
        from robert_agent.supervision_rules import timestamp
        first_failed = timestamp(data['first_failed_at'])
        poll = config.get('supervision', {}).get('poll_seconds', supervision_config.DEFAULTS['poll_seconds'])
        data['notification_eligible_at'] = (first_failed + timedelta(seconds=poll)).isoformat()
        data['delivery'] = previous.get('delivery', {}) if continuing else {}
        data['delivery_id'] = 'supervisor-health-' + hashlib.sha256(
            (str(Path(config['db_path']).resolve()) + data['first_failed_at']).encode()).hexdigest()[:24]
    atomic_json(health_path(config), data)
    if not result['ok'] and previous.get('ok', True):
        print('Robert supervision: observation unavailable; health recorded independently of the database', file=sys.stderr)


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
        recovery = []
        if record:
            with closing(connect(config['db_path'])) as conn, conn:
                conn.execute('BEGIN IMMEDIATE')
                observations = [item for item in observations if supervision_observe.is_current(conn, item)]
                changes = supervision_store.apply_observations(conn, observations, now)
                supervision_store.update_checkpoint(conn, 'service', 'supervisor', 'cursor', now=now,
                                                     metadata={'cursor': sampled.get('cursor', '')})
                supervision_actions.verify(conn, observations=observations, now=now)
                recovery = supervision_actions.plan(conn, observations=observations, now=now, config=config)
                queued_incidents = {item.get('incident_id') for item in recovery}
                _notify_changes(conn, [change for change in changes if change['incident']['incident_id'] not in queued_incidents], now)
        result = {'ok': True, 'status': 'observed', 'checked_at': now, 'observations': observations,
                  'changes': changes, 'recovery_requests': len(recovery), 'coverage_complete': sampled.get('coverage_complete', False)}
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


def owns_health_alerts(config, now):
    if not config or config.get('supervision', {}).get('mode') == 'off':
        return False
    health = _read_health(config)
    from robert_agent.supervision_rules import timestamp
    current = timestamp(now)
    checked = timestamp(health.get('checked_at'))
    complete = timestamp(health.get('last_complete_check_at'))
    poll = config.get('supervision', {}).get('poll_seconds', 60)
    return bool(health.get('ok') and current and checked and complete
                and 0 <= (current - checked).total_seconds() <= poll * 2 + 10
                and 0 <= (current - complete).total_seconds() <= max(300, poll * 10))


def status(config):
    try:
        with closing(connect(config['db_path'], readonly=True)) as conn:
            _schema_ready(conn)
            incidents = supervision_store.list_incidents(conn, status='active')
            for incident in incidents:
                analysis = supervision_store.get_checkpoint(conn, 'diagnosis', incident['incident_id'], 'analysis')
                incident['diagnosis'] = (analysis or {}).get('metadata')
                from robert_agent import supervision_repair
                incident['repair'] = supervision_repair.status(config, incident['incident_id'])
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
    from robert_agent.supervision_rules import timestamp
    checked = timestamp(health.get('checked_at'))
    eligible = timestamp(health.get('notification_eligible_at'))
    # A stale single failure is not an outage: require a later failed observation.
    if health.get('consecutive_failures', 0) < 2 or not checked or not eligible or checked < eligible:
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
        maintenance = None
        maintenance_started = 0.0
        try:
            while not stop.is_set():
                result = check(config, record=True)
                if result['ok']:
                    result['diagnosis'] = supervision_diagnose.poll(config)
                if delivery is not None and delivery.poll() is not None:
                    delivery.wait()
                    delivery = None
                if delivery is None and os.environ.get('ROBERT_NOTIFICATION_COMMAND'):
                    delivery = subprocess.Popen([sys.executable, '-m', 'robert_agent.supervisor',
                        '--deliver-once', str(config['db_path']), str(health_path(config))], stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                if maintenance is not None and maintenance.poll() is not None:
                    maintenance.wait()
                    maintenance = None
                if maintenance is not None and time.monotonic() - maintenance_started > 360:
                    _stop_child(maintenance)
                    maintenance = None
                if result['ok'] and settings.get('repair') and maintenance is None and not once:
                    maintenance = subprocess.Popen([sys.executable, '-m', 'robert_agent.supervisor',
                        '--repair-once', str(Path(config['config_path']).resolve())], stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                    maintenance_started = time.monotonic()
                if once:
                    return result
                stop.wait(settings['poll_seconds'])
            return {'ok': True, 'status': 'stopped'}
        finally:
            supervision_diagnose.shutdown(config)
            if maintenance is not None:
                _stop_child(maintenance)
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


def _stop_child(proc):
    if proc.poll() is not None:
        proc.wait()
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def runtime_status(config):
    try:
        path = runtime_dir(config) / 'daemon.json'
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as stream:
            import stat
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_uid not in {0, os.geteuid()}:
                return None
            value = json.loads(stream.read(32768))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _repair_once(config_path):
    from robert_agent import supervision_repair, supervision_repair_followup, validate_config
    def stopped(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stopped)
    signal.signal(signal.SIGINT, stopped)
    config = validate_config.validate_config(config_path, skip_external=True)
    if config.get('ok'):
        supervision_repair.poll(config)
        supervision_repair_followup.poll(config, runtime=runtime_status(config))


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--repair-once':
        _repair_once(sys.argv[2])
    elif len(sys.argv) == 4 and sys.argv[1] == '--deliver-once':
        deliver_health(sys.argv[2], Path(sys.argv[3]))
        try:
            if Path(sys.argv[2]).is_file():
                notify.deliver_pending(sys.argv[2], limit=1)
        except (OSError, sqlite3.Error):
            pass
    else:
        raise SystemExit('Use robert supervisor run/check/status')
