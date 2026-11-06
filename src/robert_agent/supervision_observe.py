"""Collect bounded supervision evidence without advancing Robert's workflows."""

from collections import Counter
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

from robert_agent import (audit_result, daemon_state, execution_store, github_operator, result_spool,
                          retry_policy, route, supervision_rules, worker_identity)
from robert_agent.worker import heartbeat as worker_heartbeat


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
        controller['lease_expires_at'] = lease['expires_at']
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
                event = conn.execute(f"""SELECT daemon_event_id,status,created_at FROM daemon_events WHERE daemon_run_id=?
                    AND event_type IN ({kinds}) ORDER BY created_at DESC,daemon_event_id DESC LIMIT 1""",
                    (run_id, *event_types)).fetchone()
                if event and event['status'] == 'ok':
                    lane_fact.update(status='completed', event_id=event['daemon_event_id'], completed_at=event['created_at'])
        facts.append(lane_fact)
    return facts


def _task_page(conn, now, cursor, limit, config):
    has_incidents = conn.execute("SELECT 1 FROM sqlite_master WHERE name='supervision_incidents' AND type='table'").fetchone()
    with_incident = """OR EXISTS (SELECT 1 FROM supervision_incidents i WHERE i.scope_type='task'
          AND i.scope_id=t.task_id AND i.status NOT IN ('resolved','dismissed'))""" if has_incidents else ''
    repos = [repo['full_name'] for repo in config.get('repos', ())]
    repo_filter = 'AND r.full_name IN (' + ','.join('?' for _ in repos) + ')' if repos else ''
    return conn.execute(f"""SELECT t.task_id,t.lifecycle,t.updated_at AS task_updated_at,t.route_id,t.expected_output,
        w.lifecycle AS stream_lifecycle,w.active_task_id,w.repo_id,r.full_name,r.enabled AS repo_enabled,
        COALESCE(lower(s.state)='closed',0) AS source_closed,
        EXISTS(SELECT 1 FROM work_items wi WHERE wi.workstream_id=w.workstream_id AND wi.canceled_at IS NOT NULL) AS canceled,
        {execution_store.runnable_sql()} AS runnable,
        json_extract(t.metadata_json,'$.supervision_lineage_key') AS lineage_key,
        json_extract(t.metadata_json,'$.dispatch_wait') AS dispatch_wait,
        json_extract(t.metadata_json,'$.wait_state.lease_expires_at') AS repository_busy_until,
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
        a.worktree_path,
        json_extract(a.metadata_json,'$.dispatch') AS dispatch,
        json_extract(a.metadata_json,'$.process_stopped_at') AS process_stopped_at,
        json_extract(a.metadata_json,'$.resume.not_before_at') AS dispatch_not_before,
        wr.result_id,wr.output_type,wr.created_at AS result_created_at,
        json_extract(wr.metadata_json,'$.audit.status') AS audit_status,
        json_extract(wr.metadata_json,'$.audit.audited_at') AS audited_at,
        json_extract(wr.metadata_json,'$.audit.action_scope') AS audited_scope,
        json_extract(wr.metadata_json,'$.normalized_payload.planned_github_actions') AS planned_actions,
        json_extract(wr.metadata_json,'$.audit_observation.next_retry_at') AS audit_retry_at
        FROM attempts a LEFT JOIN worker_results wr ON wr.task_id=a.task_id AND wr.attempt_id=a.attempt_id
        WHERE a.task_id=? ORDER BY a.attempt_no DESC LIMIT 1""", (task_id,)).fetchone()


def _publication_facts(conn, facts, now):
    actions = conn.execute(f"""SELECT a.action_id,a.task_id,a.action_type,a.audit_status,a.publish_status,a.created_at,
        a.external_id,a.target_url,a.metadata_json,
        pi.status AS intent_status,json_extract(pi.remote_evidence_json,'$.receipt') AS intent_receipt,
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
                     action_eligible_at=eligible_at.isoformat() if eligible_at else None)
    if facts['lifecycle'] == 'completed' or (actions and all(a['publish_status'] == 'published' for a in actions)):
        _delivery_facts(facts, actions)
        if actions and facts['delivery_status'] == 'confirmed':
            facts['publication_receipt'] = facts['delivery_positive']


def _delivery_facts(facts, actions):
    def outcome(status, reason, positive=None):
        facts.update(delivery_status=status, delivery_reason=reason, delivery_positive=positive)

    facts['action_count'] = len(actions)
    if len(actions) > MAX_ACTIONS_PER_RESULT or not facts['expected_output'] or facts['planned_actions'] is None:
        return outcome('unknown', 'delivery_plan_unverified')
    plan = json.loads(facts['planned_actions'])
    if not isinstance(plan, list) or not all(isinstance(action, dict) and isinstance(action.get('type'), str) for action in plan):
        return outcome('unknown', 'delivery_plan_unverified')
    facts['planned_action_count'] = len(plan)
    expected = facts['expected_output']
    actual_output = facts['output_type']
    if ((actual_output != expected and actual_output != 'waiting_for_user')
            or audit_result._audit_expected_actions(actual_output, [a['type'] for a in plan])):
        return outcome('inconsistent', 'delivery_output_contract_mismatch')
    actual = []
    publications = []
    for action in actions:
        payload = _object(action['metadata_json'])
        publication = payload.pop('publish', {})
        publications.append(publication if isinstance(publication, dict) else {})
        if payload.get('type') != action['action_type'] or action['task_id'] != facts['task_id']:
            return outcome('inconsistent', 'delivery_action_binding_mismatch')
        actual.append(payload)
    if Counter(map(supervision_rules._digest, plan)) != Counter(map(supervision_rules._digest, actual)):
        return outcome('inconsistent', 'planned_actions_do_not_match')
    if facts['audit_status'] != 'accepted' or any(a['audit_status'] != 'accepted' for a in actions):
        return outcome('inconsistent', 'completed_delivery_not_audited')
    if any(a['publish_status'] == 'skipped' for a in actions):
        # Existing business finalization permits superseded review/vote and remediation.
        return outcome('retired', 'delivery_action_retired')
    if any(a['publish_status'] != 'published' for a in actions):
        return outcome('inconsistent', 'completed_delivery_not_published')
    scope = _object(facts['audited_scope'])
    for action, publish in zip(actions, publications):
        receipt = publish.get('receipt')
        if not isinstance(receipt, dict) or receipt.get('schema_version') != 1 or not scope or not action['intent_receipt']:
            return outcome('unknown', 'delivery_receipt_unverified')
        if not _receipt_matches(facts, action, scope, receipt):
            return outcome('inconsistent', 'delivery_receipt_binding_mismatch')
    return outcome('confirmed', 'delivery_verified' if actions else 'local_delivery_verified',
                   {'result_id': facts['result_id'], 'action_ids': [a['action_id'] for a in actions]})


def _receipt_matches(facts, action, scope, receipt):
    if (action['intent_status'] != 'published' or _object(action['intent_receipt']) != receipt
            or receipt.get('scope') != scope or receipt.get('result_id') != facts['result_id']
            or not supervision_rules.timestamp(receipt.get('confirmed_at'))
            or receipt.get('confirmation_method') not in {
                'remote_reconciliation', 'remote_deduplication', 'publisher_acknowledgement'}
            or any(receipt.get(key) != action[key] for key in ('action_id', 'task_id', 'action_type'))):
        return False
    if action['action_type'] == 'push_existing_pr':
        return bool(scope.get('result_head_sha') and receipt.get('result_head_sha') == scope['result_head_sha'])
    return bool(receipt.get('external_id') and str(receipt['external_id']) == action['external_id']
                and receipt.get('target_url') and receipt['target_url'] == action['target_url'])


def _checkpoints(conn, scope_type, scope_id):
    rows = conn.execute("""SELECT rule_id,evidence_json,eligible_since FROM supervision_checkpoints
        WHERE scope_type=? AND scope_id=? AND rule_id!='admission'""", (scope_type, scope_id)).fetchall()
    return {row['rule_id']: {'evidence': _object(row['evidence_json']), 'eligible_since': row['eligible_since']} for row in rows}


def _task_facts(conn, row, controller_wait, now, capacity):
    facts = dict(row)
    if not facts['repo_enabled']:
        facts['runnable'] = False
    facts.update(controller_wait)
    facts['checkpoints'] = _checkpoints(conn, 'task', facts['task_id'])
    if supervision_rules._future(supervision_rules.timestamp(now), facts['repository_busy_until']):
        facts['local_wait'] = 'repository_busy'
    handoff = _object(facts.pop('pending_handoff'))
    attempt = _attempt_facts(conn, facts['task_id'])
    if attempt:
        facts.update(dict(attempt))
        facts['dispatch'] = _object(facts['dispatch'])
        facts['process_recorded'] = type(facts['dispatch'].get('pid')) is int
        facts['audit_eligible_at'] = facts['audit_retry_at'] or facts['process_stopped_at'] or facts['finished_at']
        if facts['result_id']:
            _publication_facts(conn, facts, now)
        if facts['attempt_status'] == 'prepared':
            _dispatch_facts(conn, facts, capacity)
        elif facts['attempt_status'] in {'running', 'stale'} and not facts['result_id']:
            _milestones(conn, facts)
    facts['generation'] = {'kind': 'task', **{key: facts.get(key) for key in
                           ('lifecycle', 'task_updated_at', 'attempt_id', 'attempt_status', 'result_id', 'audit_status')}}
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


def _dispatch_facts(conn, facts, capacity):
    facts['has_prompt'] = bool(conn.execute("""SELECT 1 FROM artifacts WHERE task_id=? AND artifact_type='prompt'
        AND (attempt_id=? OR attempt_id IS NULL) LIMIT 1""", (facts['task_id'], facts['attempt_id'])).fetchone())
    route_info = route.ROUTES.get(facts['route_id'], {})
    facts['has_worktree'] = bool(facts['worktree_path'] or not route_info.get('needs_worktree', True))
    facts['dispatch_block'] = ('worker_unavailable' if facts['dispatch_wait'] else capacity(facts['repo_id'], facts['full_name']))


def _milestones(conn, facts):
    rows = conn.execute("""SELECT artifact_type,sha256,created_at FROM artifacts WHERE task_id=? AND attempt_id=?
        AND (artifact_type LIKE 'review_%' OR artifact_type LIKE 'vote_%') AND sha256 IS NOT NULL
        ORDER BY created_at DESC,artifact_id DESC LIMIT 65""", (facts['task_id'], facts['attempt_id'])).fetchall()
    if rows and len(rows) <= 64:
        facts['milestones'] = sorted({(a['artifact_type'], a['sha256']) for a in rows})
        facts['milestone_observed_at'] = rows[0]['created_at']


def _receipt_page(conn, now, position, limit, config):
    repos = [repo['full_name'] for repo in config.get('repos', ())]
    repo_filter = 'AND r.full_name IN (' + ','.join('?' for _ in repos) + ')' if repos else ''
    return conn.execute(f"""SELECT c.scope_type,c.scope_id,c.lineage_key,c.metadata_json,
        COALESCE(lower(gs.state)='closed',0) AS source_closed,r.enabled AS repo_enabled
        FROM supervision_checkpoints c
        JOIN repos r ON r.repo_id=json_extract(c.metadata_json,'$.repo_id')
        LEFT JOIN github_sources gs ON gs.source_id=json_extract(c.metadata_json,'$.source_id')
        WHERE c.scope_type IN ('github_event','work_item_event') AND c.rule_id='admission'
          AND (c.scope_type,c.scope_id)>(?,?) {repo_filter}
          AND (julianday(c.updated_at)>=julianday(?)
               OR json_extract(c.metadata_json,'$.disposition') IS NULL
               OR json_extract(c.metadata_json,'$.disposition') IN ('deferred','linked_pending')
               OR EXISTS(SELECT 1 FROM supervision_incidents i WHERE i.scope_type=c.scope_type
                    AND i.scope_id=c.scope_id AND i.status NOT IN ('resolved','dismissed')))
        ORDER BY c.scope_type,c.scope_id LIMIT ?""",
        (*position, *repos, (now - timedelta(hours=1)).isoformat(), limit)).fetchall()


def _receipt_facts(conn, row, consumer_completed, wait):
    facts = {**_object(row['metadata_json']), 'scope_type': row['scope_type'], 'scope_id': row['scope_id'],
             'lineage_key': row['lineage_key'] or None}
    scope, key = facts['scope_type'], facts['scope_id']
    facts['generation'] = {'kind': 'admission', 'metadata_digest': supervision_rules._digest(row['metadata_json'])}
    if row['source_closed'] or not row['repo_enabled']:
        wait = 'request_no_longer_active'
    elif scope == 'work_item_event' and conn.execute("""SELECT 1 FROM work_item_events e
        JOIN work_items wi ON wi.work_item_id=e.work_item_id
        WHERE e.event_id=? AND wi.canceled_at IS NOT NULL""", (key,)).fetchone():
        wait = 'request_no_longer_active'
    facts['wait_reason'] = wait
    facts['checkpoints'] = _checkpoints(conn, scope, key)
    target, target_id = facts.get('target_type'), facts.get('target_id')
    disposition = facts.get('disposition')
    if target in {'review_round', 'vote_round'}:
        table = 'review_sessions' if target == 'review_round' else 'vote_sessions'
        facts['target_exists'] = bool(conn.execute(f"""SELECT 1 FROM {table} n JOIN repos r ON r.full_name=n.repo
            WHERE n.round_id=? AND r.repo_id=?""", (target_id, facts.get('repo_id'))).fetchone())
    elif target == 'task':
        owner = conn.execute("""SELECT t.lifecycle,w.active_task_id,w.lifecycle AS stream_lifecycle
            FROM tasks t JOIN workstreams w ON w.workstream_id=t.workstream_id
            WHERE t.task_id=? AND w.repo_id=?""", (target_id, facts.get('repo_id'))).fetchone()
        facts['target_exists'] = bool(owner)
        if owner and disposition == 'linked_pending' and scope == 'github_event':
            successor = conn.execute("""SELECT te.task_id FROM task_events te JOIN tasks t ON t.task_id=te.task_id
                WHERE te.event_id=? AND ((te.task_id=? AND te.relationship='consumed') OR
                    (t.parent_task_id=? AND te.relationship='trigger' AND t.lifecycle NOT IN ('canceled','ignored')))
                LIMIT 1""", (key, target_id, target_id)).fetchone()
            facts['successor_task_id'] = successor['task_id'] if successor else None
            facts['pending_eligible'] = (owner['lifecycle'] == 'completed' and owner['active_task_id'] == target_id
                                         and owner['stream_lifecycle'] == 'active')
        elif owner and scope == 'work_item_event':
            facts['operator_handoff'] = True
            if owner['lifecycle'] == 'detected':
                pending = github_operator.pending_tasks(conn, task_id=target_id)
                if pending and not github_operator.blocked(conn, pending[0]):
                    observed, response = github_operator.source_read(conn, pending[0])
                    facts['operator_handoff_eligible'] = bool(observed and response)
            elif conn.execute("SELECT 1 FROM attempts WHERE task_id=? LIMIT 1", (target_id,)).fetchone():
                facts['successor_task_id'] = target_id
    if disposition in {None, 'deferred', 'linked_pending'} or facts.get('operator_handoff_eligible'):
        facts['consumer_completed_at'] = consumer_completed(facts.get('repo_id'), facts.get('consumer'))
    return facts


def _page_positions(cursor):
    if not cursor.startswith('{'):
        return {'task': cursor, 'receipt': ['', ''], 'next': 'task', 'done': []}
    value = json.loads(cursor)
    if (not isinstance(value, dict) or set(value) != {'task', 'receipt', 'next', 'done'}
            or not isinstance(value['task'], str) or not isinstance(value['receipt'], list)
            or len(value['receipt']) != 2 or not all(isinstance(key, str) for key in value['receipt'])
            or value['next'] not in ('task', 'receipt') or not isinstance(value['done'], list)
            or any(kind not in ('task', 'receipt') for kind in value['done'])):
        raise ValueError('invalid supervision cursor')
    return value


def _mix_pages(pages, first, limit):
    positions = {'task': 0, 'receipt': 0}
    kind = first
    for _ in range(limit):
        other = 'receipt' if kind == 'task' else 'task'
        if positions[kind] >= len(pages[kind]):
            kind, other = other, kind
        if positions[kind] >= len(pages[kind]):
            return
        yield kind, pages[kind][positions[kind]]
        positions[kind] += 1
        kind = other


def _command_facts(db_path, facts, pgid, deadline):
    # The worker writes records to its standard artifact directory even when
    # command output itself is redirected. Never read stdout or stderr here.
    task, attempt = facts['task_id'], facts['attempt_id']
    if worker_heartbeat._safe_filename_part(task) != task:
        return
    prefix = worker_heartbeat._safe_filename_part(attempt) + '.'
    failures = Counter()
    limited = False
    try:
        with os.scandir(worker_heartbeat._default_output_dir(db_path, task)) as entries:
            for number, entry in enumerate(entries):
                if number >= 64 or time.monotonic() >= deadline:
                    limited = True
                    break
                if not entry.name.startswith(prefix) or not entry.name.endswith('.command.json'):
                    continue
                try:
                    fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, 'rb') as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            continue
                        data = stream.read(65537)
                    record = json.loads(data) if len(data) <= 65536 else {}
                except (OSError, ValueError, RecursionError):
                    limited = True
                    continue
                if not isinstance(record, dict) or (record.get('task_id'), record.get('attempt_id')) != (task, attempt):
                    continue
                if record.get('status') in {'running', 'starting'}:
                    wrapper_pid = record.get('wrapper_pid')
                    try:
                        if type(wrapper_pid) is int and wrapper_pid > 1 and os.getpgid(wrapper_pid) == pgid:
                            facts['command_in_progress'] = True
                    except OSError:
                        pass
                elif record.get('status') == 'failed':
                    signature = supervision_rules._digest([record.get('phase'), record.get('argv'), record.get('returncode')])
                    failures[signature] += 1
    except OSError:
        limited = True
    facts['command_failures'] = max(failures.values(), default=0)
    facts['command_observation'] = 'limited' if limited else 'journals_sampled'


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


def is_current(conn, observation):
    """Check receipt/attempt versions before persisting a decisive observation."""
    generation = observation.get('generation')
    if not generation:
        return True
    scope = observation['scope_id']
    if generation.get('kind') == 'admission':
        row = conn.execute("""SELECT metadata_json FROM supervision_checkpoints
            WHERE scope_type=? AND scope_id=? AND rule_id='admission'""", (observation['scope_type'], scope)).fetchone()
        return bool(row and supervision_rules._digest(row[0]) == generation['metadata_digest'])
    row = conn.execute('SELECT lifecycle,updated_at FROM tasks WHERE task_id=?', (scope,)).fetchone()
    if not row or tuple(row) != (generation['lifecycle'], generation['task_updated_at']):
        return False
    attempt = conn.execute("""SELECT a.attempt_id,a.status,wr.result_id,json_extract(wr.metadata_json,'$.audit.status')
        FROM attempts a LEFT JOIN worker_results wr ON wr.task_id=a.task_id AND wr.attempt_id=a.attempt_id
        WHERE a.task_id=? ORDER BY a.attempt_no DESC LIMIT 1""", (scope,)).fetchone()
    current = tuple(attempt) if attempt else (None, None, None, None)
    return current == tuple(generation[key] for key in ('attempt_id', 'attempt_status', 'result_id', 'audit_status'))


def collect(db_path, *, now, limit=100, config=None, cursor=''):
    """Return a page of tasks/admissions and the constant-size controller projection.

    Candidates share one read-only snapshot. Independent keysets alternate so
    a backlog of tasks cannot starve old admissions or vice versa.
    """
    now = supervision_rules.timestamp(now)
    if now is None or type(limit) is not int or not 1 <= limit <= 500 or not isinstance(cursor, str):
        raise ValueError('invalid observation time, limit or cursor')
    positions = _page_positions(cursor)
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
            pages = {
                'task': [] if 'task' in positions['done'] else _task_page(conn, now, positions['task'], limit + 1, config),
                'receipt': [] if 'receipt' in positions['done'] else _receipt_page(conn, now, positions['receipt'], limit + 1, config),
            }
            repos = {repo['full_name']: repo for repo in config.get('repos', ())}
            max_concurrency = config.get('max_concurrency')

            @lru_cache(maxsize=1)
            def full():
                if max_concurrency is None or not repos:
                    return False
                placeholders = ','.join('?' for _ in repos)
                repo_ids = [row[0] for row in conn.execute(
                    f'SELECT repo_id FROM repos WHERE full_name IN ({placeholders})', tuple(repos))]
                return daemon_state.capacity_full_for_repos(conn, repo_ids, max_concurrency)

            @lru_cache(maxsize=limit)
            def capacity(repo_id, full_name):
                if full():
                    return 'global_capacity'
                repo_limit = repos.get(full_name, {}).get('max_concurrency') or max_concurrency
                if repo_limit is not None and daemon_state.capacity_full(conn, repo_id, repo_limit):
                    return 'repository_capacity'
                return None

            @lru_cache(maxsize=limit)
            def consumer_completed(repo_id, consumer):
                if consumer not in (None, 'github_route'):
                    return None
                row = conn.execute("""SELECT MAX(finished_at) FROM run_repo_steps
                    WHERE repo_id=? AND step_key='route' AND status='succeeded'""", (repo_id,)).fetchone()
                return row[0]

            candidates = [(kind, _task_facts(conn, row, controller_wait, now.isoformat(), capacity)
                           if kind == 'task' else _receipt_facts(conn, row, consumer_completed, controller_wait.get('local_wait')))
                          for kind, row in _mix_pages(pages, positions['next'], limit)]
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
    visited = {'task': 0, 'receipt': 0}
    stall = settings.get('stall_seconds', 300)
    for kind, facts in candidates:
        if any(visited.values()) and time.monotonic() >= deadline:
            break
        visited[kind] += 1
        positions['next'] = 'receipt' if kind == 'task' else 'task'
        if kind == 'receipt':
            positions[kind] = [facts['scope_type'], facts['scope_id']]
            observations.extend(supervision_rules.evaluate_admission(facts, now=now, poll_seconds=poll,
                                                                     stall_seconds=stall))
            continue
        positions[kind] = facts['task_id']
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
        if facts.get('process_status') == 'alive_same' and not facts.get('result_id'):
            _command_facts(db_path, facts, dispatch.get('pid'), deadline)
        observations.extend(supervision_rules.evaluate_task(facts, now=now, poll_seconds=poll,
                            stall_seconds=stall))
    positions['done'] = [kind for kind in pages if visited[kind] == len(pages[kind])]
    complete = len(positions['done']) == len(pages)
    return {'observations': observations, 'coverage_complete': complete,
            'cursor': '' if complete else json.dumps(positions, separators=(',', ':'))}
