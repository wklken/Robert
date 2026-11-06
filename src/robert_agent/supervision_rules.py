"""Pure supervision rules over a bounded snapshot of controller and task facts."""

from datetime import datetime, timedelta, timezone
import hashlib
import json


ACTIVE_TASKS = {'detected', 'authorized', 'classified', 'queued', 'running'}
TERMINAL_TASKS = {'completed', 'failed', 'canceled', 'ignored'}
RUNNING_ATTEMPTS = {'running', 'stale'}


def timestamp(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _old_enough(now, value, seconds):
    when = timestamp(value)
    return when is not None and now >= when + timedelta(seconds=seconds)


def _future(now, value):
    when = timestamp(value)
    return when if when is not None and when > now else None


def _observation(scope_type, scope_id, rule, facts, now, poll, classification, reason,
                 evidence, expected, *, next_at=None, positive=None):
    milestones = {key: value for key, value in evidence.items() if key != 'process_status'}
    token = hashlib.sha256(json.dumps(milestones, sort_keys=True).encode()).hexdigest()
    row = {
        'scope_type': scope_type,
        'scope_id': scope_id,
        'rule_id': rule,
        'lineage_key': facts.get('lineage_key'),
        'classification': classification,
        'reason_code': reason,
        'severity': 'error' if classification == 'confirmed' else (
            'warning' if classification in {'suspect', 'unobservable'} else 'info'),
        'evidence': evidence,
        'expected_progress': expected,
        'next_check_at': (next_at or now + timedelta(seconds=poll)).isoformat(),
        'progress_token': token,
    }
    if positive:
        row['positive_evidence'] = positive
    return row


def _stage_wait(facts, consumer):
    if facts.get('lifecycle') == 'waiting_for_user' or facts.get('stream_lifecycle') == 'waiting_for_user':
        return 'waiting_for_user'
    if facts.get('source_closed') or facts.get('canceled'):
        return 'request_no_longer_active'
    if not facts.get('runnable'):
        return 'execution_attention'
    return facts.get(consumer + '_wait')


def _worker(facts, emit, now, grace):
    lifecycle, status = facts['lifecycle'], facts.get('attempt_status')
    process = facts.get('process_status')
    evidence = {'attempt_id': facts.get('attempt_id'), 'attempt_status': status,
                'process_status': process, 'task_lifecycle': lifecycle}
    expect = {'kind': 'worker_reconciled', 'attempt_id': facts.get('attempt_id')}
    if facts.get('process_stopped_at') and status not in RUNNING_ATTEMPTS:
        if lifecycle in {'canceled', 'ignored'}:
            return emit('expected_wait', 'request_no_longer_active', evidence, expect)
        return emit('healthy', 'worker_stop_recorded', evidence, expect,
                    positive={'attempt_id': facts['attempt_id'], 'process_stopped_at': facts['process_stopped_at']})
    if process == 'alive_same':
        if lifecycle in TERMINAL_TASKS:
            if not _old_enough(now, facts.get('task_updated_at'), grace):
                return emit('expected_wait', 'worker_cleanup_grace', evidence, expect)
            return emit('confirmed', 'terminal_task_worker_still_alive', evidence, expect)
        return emit('healthy', 'worker_identity_verified', evidence, expect)
    if process == 'group_alive':
        return emit('expected_wait', 'worker_group_still_alive', evidence, expect)
    if process == 'dead' and status in RUNNING_ATTEMPTS:
        if facts.get('result_id') or facts.get('spool_status') == 'saved':
            return emit('expected_wait', 'worker_exit_with_durable_result', evidence, expect)
        if facts.get('spool_status') != 'missing':
            return emit('unobservable', 'worker_result_spool_unverified', evidence, expect)
        # A worker can exit just before normal supervision classifies it.
        anchor = facts.get('heartbeat_at') or facts.get('started_at')
        if not _old_enough(now, anchor, grace):
            return emit('suspect', 'worker_exit_pending_reconciliation', evidence, expect)
        return emit('confirmed', 'worker_exited_without_result', evidence, expect)
    if status in RUNNING_ATTEMPTS or facts.get('process_recorded') or lifecycle == 'running':
        return emit('unobservable', 'worker_identity_unverified', evidence, expect)
    return None


def _result(facts, emit, now, stall):
    result_id, spool = facts.get('result_id'), facts.get('spool_status')
    if not result_id and spool not in {'saved', 'unverified'}:
        return None
    evidence = {'attempt_id': facts.get('attempt_id'), 'result_id': result_id,
                'audit_status': facts.get('audit_status'), 'spool_status': spool}
    expect = {'kind': 'result_audited', 'attempt_id': facts.get('attempt_id'), 'result_id': result_id}
    wait = _stage_wait(facts, 'local')
    if facts.get('canceled') or facts['lifecycle'] in {'canceled', 'ignored'}:
        return emit('expected_wait', 'request_no_longer_active', evidence, expect)
    if not result_id:
        if spool == 'unverified':
            return emit('unobservable', 'worker_result_spool_unverified', evidence, expect)
        if wait:
            return emit('expected_wait', wait, evidence, expect)
        overdue = _old_enough(now, facts.get('spool_saved_at'), stall)
        return emit('suspect' if overdue else 'expected_wait', 'result_waiting_ingestion', evidence, expect)
    if facts.get('attempt_status') != 'completed' or (
            facts.get('process_recorded') and not facts.get('process_stopped_at')):
        return emit('expected_wait', 'result_waiting_worker_exit', evidence, expect)
    if facts.get('audit_status') == 'accepted':
        return emit('healthy', 'result_audit_accepted', evidence, expect,
                    positive={'result_id': result_id, 'audit_status': 'accepted'})
    if facts.get('audit_status'):
        return emit('expected_wait', 'result_audit_requires_attention', evidence, expect)
    retry = _future(now, facts.get('audit_retry_at'))
    if retry:
        return emit('expected_wait', 'result_audit_backoff', evidence, expect, next_at=retry)
    if wait or facts['lifecycle'] not in ACTIVE_TASKS:
        return emit('expected_wait', wait or 'request_no_longer_active', evidence, expect)
    overdue = _old_enough(now, facts.get('audit_eligible_at') or facts.get('result_created_at'), stall)
    return emit('suspect' if overdue else 'expected_wait', 'result_waiting_audit', evidence, expect)


def _publication(facts, emit, now, stall):
    action = facts.get('pending_action_id')
    result_id = facts.get('result_id')
    if not action and not facts.get('publication_receipt'):
        return None
    evidence = {'result_id': result_id, 'action_id': action, 'intent_status': facts.get('intent_status')}
    expect = {'kind': 'publication_confirmed', 'result_id': result_id, 'action_id': action}
    if facts.get('canceled') or facts['lifecycle'] in {'canceled', 'ignored'}:
        return emit('expected_wait', 'request_no_longer_active', evidence, expect)
    if not action and facts.get('publication_receipt'):
        return emit('healthy', 'publication_confirmed', evidence, expect,
                    positive={'result_id': result_id, **facts['publication_receipt']})
    if facts.get('intent_status') in {'executing', 'attention'}:
        return emit('expected_wait', 'publication_effect_unresolved', evidence, expect)
    retry = _future(now, facts.get('publication_retry_at'))
    if retry:
        return emit('expected_wait', 'publication_backoff', evidence, expect, next_at=retry)
    wait = _stage_wait(facts, 'publish')
    if wait or not facts.get('publication_eligible') or facts['lifecycle'] not in ACTIVE_TASKS:
        return emit('expected_wait', wait or 'publication_not_eligible', evidence, expect)
    overdue = _old_enough(now, facts.get('action_eligible_at') or facts.get('action_created_at'), stall)
    return emit('suspect' if overdue else 'expected_wait', 'publication_waiting_consumer', evidence, expect)


def _handoff(facts, emit, now, stall):
    status = facts.get('handoff_status')
    if not status:
        return None
    child = facts.get('successor_task_id')
    evidence = {'handoff_status': status, 'successor_task_id': child}
    expect = {'kind': 'handoff_successor'}
    if facts.get('canceled') or facts.get('source_closed') or facts['lifecycle'] in {'canceled', 'ignored'}:
        return emit('expected_wait', 'request_no_longer_active', evidence, expect)
    if status == 'completed' and child:
        return emit('healthy', 'handoff_successor_recorded', evidence, expect,
                    positive={'successor_task_id': child})
    if status == 'attention':
        return emit('confirmed', 'handoff_requires_attention', evidence, expect)
    if status == 'completed':
        return emit('healthy', 'handoff_no_successor_recorded', evidence, expect)
    retry = _future(now, facts.get('handoff_retry_at'))
    if retry:
        return emit('expected_wait', 'handoff_backoff', evidence, expect, next_at=retry)
    wait = _stage_wait(facts, 'local')
    if wait or not facts.get('handoff_current'):
        return emit('expected_wait', wait or 'handoff_not_eligible', evidence, expect)
    overdue = _old_enough(now, facts.get('handoff_due_at'), stall)
    return emit('suspect' if overdue else 'expected_wait', 'handoff_waiting_consumer', evidence, expect)


def evaluate_task(facts, *, now, poll_seconds=60, stall_seconds=300, worker_grace_seconds=60):
    now = timestamp(now)
    rules = (
        ('worker_state_mismatch', _worker, worker_grace_seconds),
        ('result_not_consumed', _result, stall_seconds),
        ('publication_stalled', _publication, stall_seconds),
        ('handoff_stalled', _handoff, stall_seconds),
    )
    rows = []
    for rule, classify, age in rules:
        def emit(classification, reason, evidence, expected, **kwargs):
            return _observation('task', facts['task_id'], rule, facts, now, poll_seconds,
                                classification, reason, evidence, expected, **kwargs)
        row = classify(facts, emit, now, age)
        if row:
            rows.append(row)
    return rows


def evaluate_controller(facts, *, now, poll_seconds=60):
    now = timestamp(now)
    scope_id = facts['scope_id']
    evidence = {'daemon_run_id': facts.get('daemon_run_id'), 'event_id': facts.get('event_id'),
                'lease_id': facts.get('lease_id')}
    expect = {'kind': 'controller_progress', 'scope_id': scope_id}
    status = facts.get('status')
    if status in {'disabled', 'stopped'}:
        outcome = ('expected_wait', 'controller_' + status)
    elif status == 'attention':
        outcome = ('confirmed', 'controller_lane_latched')
    elif status == 'failed':
        outcome = ('confirmed', 'controller_run_failed')
    elif status == 'expired':
        outcome = ('suspect', 'controller_lease_expired')
    elif status in {'fresh', 'completed'}:
        outcome = ('healthy', 'controller_progress_observed')
    else:
        outcome = ('unobservable', 'controller_progress_unverified')
    positive = evidence if status == 'completed' or (status == 'fresh' and facts.get('has_progress')) else None
    return _observation('controller', scope_id, 'controller_or_notice_stalled', facts, now, poll_seconds,
                        *outcome, evidence, expect, positive=positive)
