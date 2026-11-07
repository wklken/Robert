"""Real HTTP provider throttling for the independent diagnosis process."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from robert_agent.adapters.sqlite import database as storage
from robert_agent.supervision import diagnosis as supervision_diagnose
from robert_agent.supervision import store as supervision_store
from tests.support_model_http import model_http


WRAPPER = '''import json,sys,urllib.request,urllib.error
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
packet=json.load(sys.stdin)
try:
    with urllib.request.urlopen(sys.argv[1],timeout=3) as response:
        assert response.status==200
except urllib.error.HTTPError as error:
    if error.code!=429: raise
    failure={'code':'rate_limited'}
    value=error.headers.get('Retry-After','')
    try:
        seconds=int(value) if value.isdecimal() else max(0,int((parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds())+1)
        failure['retry_after_seconds']=seconds
    except (TypeError,ValueError,OverflowError):
        pass
    json.dump({'bundle_id':packet['bundle_id'],'bundle_sha256':packet['bundle_sha256'],'error':failure},sys.stdout)
    sys.exit(75)
json.dump({'bundle_id':packet['bundle_id'],'bundle_sha256':packet['bundle_sha256'],
    'diagnosis_class':'expected_wait','recommended_action':'wait',
    'evidence_ids':[packet['evidence'][0]['id']],'expected_progress':'Observe a committed result'},sys.stdout)
'''


def configure(root, db, url, *, budget=3):
    root.mkdir(parents=True, exist_ok=True)
    wrapper = root / 'diagnose.py'
    wrapper.write_text(WRAPPER)
    return {'db_path': str(db), 'data_dir': str(root), 'supervision': {'mode': 'observe',
        'diagnosis': {'command': [sys.executable, str(wrapper), url], 'timeout_seconds': 5,
                      'cooldown_seconds': 2, 'max_calls_per_hour': budget}}}


def incident(db, scope):
    with closing(sqlite3.connect(db)) as conn, conn:
        supervision_store.apply_observations(conn, [{'scope_type':'controller','scope_id':scope,
            'rule_id':'controller_or_notice_stalled','classification':'confirmed','severity':'error',
            'reason_code':'controller_progress_stalled','evidence':{'run':scope},
            'expected_progress':{'kind':'controller_tick'}}], datetime.now(timezone.utc).isoformat())


def wait(config, expected, *, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = supervision_diagnose.poll(config)
        if result['status'] == expected:
            return result
        assert result['status'] in {'launched', 'running', 'cooldown', 'provider_cooldown'}, result
        time.sleep(.02)
    raise AssertionError(f'Diagnosis did not reach {expected}: {result}')


def restart_poll(config, *, now=None):
    result = subprocess.run([sys.executable, '-I', '-c',
        'import json,sys; from robert_agent.supervision import diagnosis as supervision_diagnose; '
        'print(json.dumps(supervision_diagnose.poll(json.loads(sys.argv[1]),now=json.loads(sys.argv[2]))))',
        json.dumps(config), json.dumps(now)],
        capture_output=True, text=True, timeout=5, check=True)
    return json.loads(result.stdout)


def runner(db):
    with closing(sqlite3.connect(db)) as conn:
        return supervision_store.get_checkpoint(conn, 'diagnostic', 'installation', 'runner')['metadata']


def assert_no_effect(db):
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute('SELECT count(*) FROM supervision_recovery_actions').fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM supervision_incidents WHERE status='resolved'").fetchone()[0] == 0


def recovered(root, header='3', *, kind='seconds'):
    root.mkdir()
    db = root / 'state.db'
    storage.init_database(db)
    if kind == 'http_date':
        header = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=5), usegmt=True)
    with model_http({'/diagnosis': [(429, header), (200, None)]}) as endpoint:
        config = configure(root, db, endpoint.url + '/diagnosis')
        incident(db, 'first')
        try:
            wait(config, 'provider_rate_limited')
            retry_at = datetime.fromisoformat(runner(db)['provider_not_before']).timestamp()
            incident(db, 'other')
            during_cooldown = (datetime.fromisoformat(runner(db)['provider_not_before']) - timedelta(seconds=1)).isoformat()
            assert restart_poll(config, now=during_cooldown)['status'] == 'provider_cooldown'
            assert len(endpoint.requests()) == 1
            wait(config, 'succeeded')
            calls = endpoint.requests()
            assert [call['status'] for call in calls] == [429, 200], calls
            assert calls[1]['wall_at'] >= retry_at, calls
            if kind == 'seconds':
                assert calls[1]['at'] - calls[0]['at'] >= int(header), calls
            elif kind == 'http_date':
                assert calls[1]['wall_at'] >= parsedate_to_datetime(header).timestamp(), calls
            else:
                assert calls[1]['at'] - calls[0]['at'] >= 2, calls
            assert len(runner(db)['hourly_calls']) == 2 and runner(db)['claim'] is None
            assert_no_effect(db)
            return {'scenario': 'diagnosis_429_recovery_' + kind,
                    'passed': True, 'http_statuses': [call['status'] for call in calls],
                    'checks': ['real_http', 'global_cooldown_across_incidents_and_restart',
                               'cooldown_or_retry_after_respected', 'bounded_budget', 'no_business_effect']}
        finally:
            supervision_diagnose.shutdown(config)


def exhausted(root):
    root.mkdir()
    db = root / 'state.db'
    storage.init_database(db)
    with model_http({'/diagnosis': [(429, '1')]}) as endpoint:
        config = configure(root, db, endpoint.url + '/diagnosis', budget=2)
        incident(db, 'first')
        try:
            wait(config, 'provider_rate_limited')
            wait(config, 'provider_rate_limited')
            wait(config, 'rate_limited')
            assert restart_poll(config)['status'] == 'rate_limited'
            for _ in range(3):
                assert supervision_diagnose.poll(config)['status'] == 'rate_limited'
            assert len(endpoint.requests()) == 2
            assert runner(db)['claim'] is None
            assert_no_effect(db)
            return {'scenario':'diagnosis_persistent_429_budget_and_restart', 'passed':True,
                    'http_requests':2, 'checks':['hourly_budget_not_reset', 'no_busy_retry',
                                               'no_false_product_defect_or_repair']}
        finally:
            supervision_diagnose.shutdown(config)


def simultaneous(root):
    from tests.simulations.model_rate_limit_scenarios import worker_runtime
    root.mkdir()
    routes = {'/diagnosis':[(429,'2')], '/worker/123/model-primary':[(429,'2'),(200,None)]}
    with model_http(routes) as endpoint, worker_runtime(root / 'worker', endpoint, tries=2) as runtime:
        config = configure(root / 'diagnosis', runtime.db, endpoint.url + '/diagnosis', budget=1)
        incident(runtime.db, 'while_worker_waits')
        try:
            runtime.dispatch()
            wait(config, 'provider_rate_limited')
            during_cooldown = (datetime.fromisoformat(runner(runtime.db)['provider_not_before']) - timedelta(seconds=1)).isoformat()
            assert restart_poll(config, now=during_cooldown)['status'] == 'provider_cooldown'
            runtime.observe()
            runtime.finish()
            assert len(endpoint.requests('/diagnosis')) == 1
            assert [call['status'] for call in endpoint.requests('/worker/123/model-primary')] == [429,200]
            with closing(sqlite3.connect(runtime.db)) as conn:
                assert conn.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
                assert conn.execute('SELECT count(*) FROM worker_results').fetchone()[0] == 1
                assert conn.execute('SELECT count(*) FROM github_actions').fetchone()[0] == 0
            assert_no_effect(runtime.db)
            return {'scenario':'worker_and_diagnosis_simultaneous_http_429','passed':True,
                    'worker_http_requests':2,'diagnosis_http_requests':1,
                    'checks':['observer_remains_available','worker_recovers_without_diagnostic_model',
                              'one_attempt_one_result_no_github_actions']}
        finally:
            supervision_diagnose.shutdown(config)


def run(root):
    root = Path(root) / 'diagnosis-rate-limits'
    root.mkdir()
    return [recovered(root / 'recover'), recovered(root / 'date', kind='http_date'),
            recovered(root / 'invalid', 'invalid', kind='invalid_header'),
            recovered(root / 'missing', None, kind='missing_header'),
            exhausted(root / 'exhausted'), simultaneous(root / 'simultaneous')]
