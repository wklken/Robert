"""Collect bounded supervision evidence without advancing Robert's workflows."""

from contextlib import closing
from datetime import timedelta
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import time

from robert_agent import execution_store, result_spool, retry_policy, supervision_rules, worker_identity


DB_SECONDS = 4
OBSERVATION_SECONDS = 9
MAX_SPOOL_BYTES = 256 * 1024
MAX_ACTIONS_PER_RESULT = 100
LANE_COMPLETIONS = {
    'local': ('local_drain_completed',),
    'scan': ('scan_completed', 'live_poll_completed'),
    'publish': ('publish_completed',),
}


class ObservationUnavailable(ValueError):
    """No trustworthy snapshot is available to classify."""

    def __init__(self, reason_code):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _object(value):
    decoded = json.loads(value or '{}') if isinstance(value, (str, bytes)) else None
    return decoded if isinstance(decoded, dict) else {}


def _database_error(exc):
    name = getattr(exc, 'sqlite_errorname', '')
    if name.startswith(('SQLITE_BUSY', 'SQLITE_LOCKED')):
        return 'database_busy'
    if name.startswith(('SQLITE_CORRUPT', 'SQLITE_NOTADB')):
        return 'database_corrupt'
    if name == 'SQLITE_INTERRUPT':
        return 'database_observation_deadline'
    if name == 'SQLITE_ERROR' and ('no such table' in str(exc) or 'no such column' in str(exc)):
        return 'database_schema_incompatible'
    return 'database_unavailable'


def _controller_facts(conn, db_path, now, config):
    lease = conn.execute("""SELECT l.lease_id,l.owner_id,l.status AS lease_status,l.acquired_at,l.expires_at,l.heartbeat_at,
        d.status AS run_status FROM leases l LEFT JOIN daemon_runs d ON d.daemon_run_id=l.owner_id
        WHERE l.resource_type='daemon' AND l.resource_key=?
        ORDER BY (l.status='active') DESC,l.acquired_at DESC,l.lease_id DESC LIMIT 1""",
        ('database:' + str(db_path),)).fetchone()
    configured = config.get('daemon', {}).get('enabled', True)
    status, run_id = 'unverified', lease['owner_id'] if lease else None
    if not configured:
        status = 'disabled'
    elif lease:
        expires = supervision_rules.timestamp(lease['expires_at'])
        if lease['run_status'] in {'failed', 'stopped'}:
            status = lease['run_status']
        elif expires:
            status = 'fresh' if lease['lease_status'] == 'active' and expires > now else 'expired'
    controller = {'scope_id': 'daemon', 'status': status, 'daemon_run_id': run_id}
    if lease:
        controller['lease_id'] = lease['lease_id']
        heartbeat = supervision_rules.timestamp(lease['heartbeat_at'])
        acquired = supervision_rules.timestamp(lease['acquired_at'])
        controller['has_progress'] = bool(acquired and heartbeat and acquired < heartbeat <= now)
    facts = [controller]
    for lane, event_types in LANE_COMPLETIONS.items():
        lane_fact = {'scope_id': 'daemon:' + lane, 'daemon_run_id': run_id,
                     'status': status if status in {'disabled', 'stopped'} else 'unverified'}
        if run_id and configured and status not in {'failed', 'stopped'}:
            # A same-run child completion cannot clear a daemon attention latch.
            attention = conn.execute("""SELECT daemon_event_id,json_extract(metadata_json,'$.cause_status') AS cause
                FROM daemon_events WHERE daemon_run_id=?
                AND event_type='control_plane_attention' AND json_extract(metadata_json,'$.control_lane')=?
                ORDER BY created_at DESC,daemon_event_id DESC LIMIT 1""", (run_id, lane)).fetchone()
            if attention:
                lane_fact.update(status='attention', event_id=attention['daemon_event_id'], cause=attention['cause'])
            elif status == 'fresh':
                kinds = ','.join('?' for _ in event_types)
                event = conn.execute(f"""SELECT daemon_event_id,status FROM daemon_events WHERE daemon_run_id=?
                    AND event_type IN ({kinds}) ORDER BY created_at DESC,daemon_event_id DESC LIMIT 1""",
                    (run_id, *event_types)).fetchone()
                if event and event['status'] == 'ok':
                    lane_fact.update(status='completed', event_id=event['daemon_event_id'])
        facts.append(lane_fact)
    return facts


def _task_page(conn, now, cursor, limit, config):
    has_incidents = conn.execute("SELECT 1 FROM sqlite_master WHERE name='supervision_incidents' AND type='table'").fetchone()
    with_incident = """OR EXISTS (SELECT 1 FROM supervision_incidents i WHERE i.scope_type='task'
          AND i.scope_id=t.task_id AND i.status NOT IN ('resolved','dismissed'))""" if has_incidents else ''
    repos = [repo['full_name'] for repo in config.get('repos', ())]
    repo_filter = 'AND r.full_name IN (' + ','.join('?' for _ in repos) + ')' if repos else ''
    return conn.execute(f"""SELECT t.task_id,t.lifecycle,t.updated_at AS task_updated_at,
        w.lifecycle AS stream_lifecycle,w.active_task_id,r.enabled AS repo_enabled,
        COALESCE(lower(s.state)='closed',0) AS source_closed,
        EXISTS(SELECT 1 FROM work_items wi WHERE wi.workstream_id=w.workstream_id AND wi.canceled_at IS NOT NULL) AS canceled,
        {execution_store.runnable_sql()} AS runnable,
        json_extract(t.metadata_json,'$.supervision_lineage_key') AS lineage_key,
        json_extract(t.metadata_json,'$.pending_handoff') AS pending_handoff
        FROM tasks t JOIN workstreams w ON w.workstream_id=t.workstream_id
        JOIN repos r ON r.repo_id=w.repo_id LEFT JOIN github_sources s ON s.source_id=w.primary_source_id
        WHERE t.task_id>? {repo_filter} AND (
            t.lifecycle IN ('detected','authorized','classified','queued','running','waiting_for_user')
            OR (w.active_task_id=t.task_id AND w.lifecycle='active')
            OR julianday(t.updated_at)>=julianday(?) {with_incident})
        ORDER BY t.task_id LIMIT ?""", (cursor, *repos, (now - timedelta(hours=1)).isoformat(), limit)).fetchall()


def _attempt_facts(conn, task_id):
    return conn.execute("""SELECT a.attempt_id,a.status AS attempt_status,a.started_at,a.heartbeat_at,a.finished_at,
        json_extract(a.metadata_json,'$.dispatch') AS dispatch,
        json_extract(a.metadata_json,'$.process_stopped_at') AS process_stopped_at,
        wr.result_id,wr.created_at AS result_created_at,
        json_extract(wr.metadata_json,'$.audit.status') AS audit_status,
        json_extract(wr.metadata_json,'$.audit.audited_at') AS audited_at,
        json_extract(wr.metadata_json,'$.audit_observation.next_retry_at') AS audit_retry_at
        FROM attempts a LEFT JOIN worker_results wr ON wr.task_id=a.task_id AND wr.attempt_id=a.attempt_id
        WHERE a.task_id=? ORDER BY a.attempt_no DESC LIMIT 1""", (task_id,)).fetchone()


def _publication_facts(conn, facts, now):
    actions = conn.execute(f"""SELECT a.action_id,a.audit_status,a.publish_status,a.created_at,
        pi.status AS intent_status,
        json_extract(a.metadata_json,'$.publish.next_retry_at') AS retry_at,
        json_extract(a.metadata_json,'$.publish.published_at') AS published_at,
        {retry_policy.publication_ready_sql('a')} AS eligible
        FROM github_actions a LEFT JOIN publication_intents pi ON pi.action_id=a.action_id
        WHERE a.result_id=? ORDER BY a.created_at,a.action_id LIMIT ?""",
        (now, facts['result_id'], MAX_ACTIONS_PER_RESULT + 1)).fetchall()
    pending = [action for action in actions if action['publish_status'] == 'not_published']
    if pending:
        action = next((row for row in pending if row['eligible']), pending[0])
        relevant = (action['retry_at'], facts.get('process_stopped_at'), facts.get('audited_at'), action['created_at'])
        eligible_at = max(filter(None, map(supervision_rules.timestamp, relevant)), default=None)
        facts.update(pending_action_id=action['action_id'], intent_status=action['intent_status'],
                     publication_eligible=bool(action['eligible']), publication_retry_at=action['retry_at'],
                     action_created_at=action['created_at'],
                     action_eligible_at=eligible_at)
    if actions and len(actions) <= MAX_ACTIONS_PER_RESULT and all(
        a['audit_status'] == 'accepted' and a['publish_status'] == 'published'
        and a['intent_status'] == 'published' and a['published_at'] for a in actions
    ):
        facts['publication_receipt'] = {'action_ids': [a['action_id'] for a in actions]}


def _task_facts(conn, row, controller_wait, now):
    facts = dict(row)
    if not facts['repo_enabled']:
        facts['runnable'] = False
    facts.update(controller_wait)
    handoff = _object(facts.pop('pending_handoff'))
    attempt = _attempt_facts(conn, facts['task_id'])
    if attempt:
        facts.update(dict(attempt))
        facts['dispatch'] = _object(facts['dispatch'])
        facts['process_recorded'] = type(facts['dispatch'].get('pid')) is int
        facts['audit_eligible_at'] = facts['audit_retry_at'] or facts['process_stopped_at'] or facts['finished_at']
        if facts['result_id']:
            _publication_facts(conn, facts, now)
    if handoff:
        child = conn.execute("SELECT task_id FROM tasks WHERE parent_task_id=? AND task_id=? AND lifecycle NOT IN ('canceled','ignored')",
                             (facts['task_id'], handoff.get('child_task_id'))).fetchone()
        facts.update(handoff_status=handoff.get('status'), handoff_retry_at=handoff.get('next_retry_at'),
                     successor_task_id=child['task_id'] if child else None,
                     handoff_due_at=handoff.get('next_retry_at') or facts['task_updated_at'],
                     handoff_current=(facts['active_task_id'] == facts['task_id']
                                      and facts['stream_lifecycle'] == 'active'
                                      and handoff.get('retryable') is not False))
    return facts


def _observe_process(dispatch, ps_runner, live_groups):
    pid = dispatch.get('pid')
    if type(pid) is not int or pid <= 1:
        return 'unknown'
    if dispatch.get('process_exited'):
        identity = 'dead'
    else:
        identity = worker_identity.inspect_recorded_identity(dispatch, ps_runner=ps_runner)
    if identity != 'dead':
        return identity
    groups = live_groups()
    if groups is None:
        return 'unknown'
    return 'group_alive' if pid in groups else 'dead'


def _spool_facts(db_path, task_id, attempt_id):
    key = hashlib.sha256(attempt_id.encode()).hexdigest()
    path = result_spool.directory(db_path) / (key + '.json')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return {'spool_status': 'unverified'}
            raw = stream.read(MAX_SPOOL_BYTES + 1)
        if len(raw) > MAX_SPOOL_BYTES:
            return {'spool_status': 'unverified'}
        record = json.loads(raw)
        result_spool._validate_record(record)
        if record['payload'].get('task_id') == task_id and record['payload'].get('attempt_id') == attempt_id:
            return {'spool_status': 'saved', 'spool_saved_at': record.get('saved_at')}
    except FileNotFoundError:
        return {'spool_status': 'missing'}
    except (OSError, ValueError, RecursionError):
        pass
    return {'spool_status': 'unverified'}


def _dead_worker_snapshot_current(db_path, facts, dispatch):
    # Spool replay may have committed a result and removed its file since the
    # first snapshot. Do not report a result-less exit from that mixed view.
    with closing(sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=0.1)) as conn:
        conn.row_factory = sqlite3.Row
        fresh = _attempt_facts(conn, facts['task_id'])
    return bool(fresh and not fresh['result_id'] and not fresh['process_stopped_at']
                and all(fresh[key] == facts.get(key) for key in ('attempt_id', 'attempt_status', 'heartbeat_at'))
                and _object(fresh['dispatch']) == dispatch)


def collect(db_path, *, now, limit=100, config=None, cursor=''):
    """Return one task-ID page and the constant-size controller projection.

    Candidates and related facts share one read-only SQLite snapshot. A single
    keyset cursor advances only over evaluated tasks and resets at a full pass.
    """
    now = supervision_rules.timestamp(now)
    if now is None or type(limit) is not int or not 1 <= limit <= 500 or not isinstance(cursor, str):
        raise ValueError('invalid observation time, limit or cursor')
    config = config or {}
    settings = config.get('supervision', {})
    poll = settings.get('poll_seconds', 60)
    db_path = Path(db_path).expanduser().resolve()
    started = time.monotonic()
    deadline = started + OBSERVATION_SECONDS
    db_deadline = started + DB_SECONDS
    try:
        with closing(sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=0.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.set_progress_handler(lambda: int(time.monotonic() >= db_deadline), 1000)
            conn.execute('BEGIN')
            controllers = _controller_facts(conn, db_path, now, config)
            controller_wait = {}
            if controllers[0]['status'] != 'fresh' and config:
                controller_wait.update(local_wait='controller_unavailable', publish_wait='controller_unavailable')
            for facts in controllers[1:]:
                if facts['status'] == 'attention':
                    lane = facts['scope_id'].split(':', 1)[1]
                    controller_wait[lane + '_wait'] = 'controller_lane_latched'
                    if facts.get('cause') == 'blocked_result_contract_cutover':
                        controller_wait.update(local_wait='result_contract_cutover', publish_wait='result_contract_cutover')
            candidates = _task_page(conn, now, cursor, limit + 1, config)
            tasks = [_task_facts(conn, row, controller_wait, now.isoformat()) for row in candidates[:limit]]
    except sqlite3.Error as exc:
        raise ObservationUnavailable(_database_error(exc)) from exc
    except (ValueError, RecursionError) as exc:
        raise ObservationUnavailable('database_evidence_invalid') from exc

    def ps_runner(command, **kwargs):
        available = deadline - time.monotonic()
        if available <= 0:
            raise subprocess.TimeoutExpired(command, 0)
        kwargs['timeout'] = min(kwargs.get('timeout', 2), available)
        return subprocess.run(command, **kwargs)

    @lru_cache(maxsize=1)
    def live_groups():
        try:
            result = ps_runner(['ps', '-axo', 'pgid=,stat='], capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode:
            return None
        return {int(fields[0]) for line in result.stdout.splitlines()
                if len(fields := line.split()) == 2 and fields[0].isdigit() and fields[1][0] not in 'ZXx'}

    observations = [supervision_rules.evaluate_controller(facts, now=now, poll_seconds=poll) for facts in controllers]
    visited = 0
    for facts in tasks:
        if visited and time.monotonic() >= deadline:
            break
        dispatch = facts.pop('dispatch', {})
        if dispatch and not facts.get('process_stopped_at'):
            facts['process_status'] = _observe_process(dispatch, ps_runner, live_groups)
        if facts.get('attempt_id') and not facts.get('result_id') and facts.get('attempt_status') in {'running', 'stale', 'prepared'}:
            facts.update(_spool_facts(db_path, facts['task_id'], facts['attempt_id']))
        if facts.get('process_status') == 'dead' and facts.get('spool_status') == 'missing':
            try:
                current = time.monotonic() < deadline and _dead_worker_snapshot_current(db_path, facts, dispatch)
            except (sqlite3.Error, ValueError):
                current = False
            if not current:
                facts['spool_status'] = 'unverified'
        observations.extend(supervision_rules.evaluate_task(facts, now=now, poll_seconds=poll,
                            stall_seconds=settings.get('stall_seconds', 300)))
        visited += 1
    complete = visited == len(candidates)
    return {'observations': observations, 'coverage_complete': complete,
            'cursor': '' if complete else tasks[visited - 1]['task_id']}
