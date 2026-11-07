from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.adapters.sqlite import database as storage
from robert_agent.supervision import store as supervision_store
from robert_agent.adapters.processes import identity as worker_identity


VALID_WRAPPER = '''
import hashlib, json, os, pathlib, sys
bundle = json.load(sys.stdin)
digest = bundle.pop('bundle_sha256')
assert digest == hashlib.sha256(json.dumps(bundle, ensure_ascii=False, sort_keys=True,
                                           separators=(',', ':')).encode()).hexdigest()
assert pathlib.Path(os.environ['HOME']) == pathlib.Path.cwd()
assert pathlib.Path.cwd().stat().st_mode & 0o777 == 0o700
assert 'GH_TOKEN' not in os.environ
assert 'CODEX_HOME' not in os.environ
assert 'ROBERT_CONFIG' not in os.environ
assert os.environ.get('MODEL_FIXTURE') == 'allowed-for-test'
encoded = json.dumps(bundle, ensure_ascii=False)
assert 'Check the original target' in encoded
assert 'very-secret-credential' not in encoded
assert 'unrelated objective' not in encoded
json.dump({'bundle_id': bundle['bundle_id'], 'bundle_sha256': digest,
           'diagnosis_class': 'repeated_failure', 'evidence_ids': [bundle['evidence'][0]['id']],
           'recommended_action': 'reconcile', 'expected_progress': 'Observe a matching receipt'}, sys.stdout)
'''


class DiagnosisTests(unittest.TestCase):
    def setUp(self):
        from robert_agent.supervision import diagnosis as supervision_diagnose

        self.diagnose = supervision_diagnose
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / 'state.sqlite3'
        storage.init_database(self.db)
        self.when = datetime.now(timezone.utc)
        self.config = {'db_path': str(self.db), 'data_dir': str(self.root), 'supervision': {'mode': 'observe'}}
        self.addCleanup(self.diagnose.shutdown, self.config)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('r','acme/robert','bot','main','/tmp/repo','/tmp/trees')")
            conn.execute("INSERT INTO workstreams(workstream_id,repo_id,created_at,updated_at) VALUES ('w','r',?,?),('other','r',?,?)", [self.when.isoformat()] * 4)
            conn.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES ('target','w','running',?,?)", [self.when.isoformat()] * 2)
            conn.execute("""INSERT INTO work_items(work_item_id,repo_id,title,description,origin_type,workstream_id,
                creation_idempotency_key,created_by,created_at,updated_at)
                VALUES ('wi','r','Check the original target','password = very-secret-credential','web','w','key','operator',?,?),
                       ('decoy','r','unrelated objective','','web','other','other-key','operator',?,?)""", [self.when.isoformat()] * 4)
            changes = supervision_store.apply_observations(conn, [{
                'scope_type': 'task', 'scope_id': 'target', 'rule_id': 'publication_stalled',
                'lineage_key': 'lineage', 'classification': 'confirmed', 'reason_code': 'publication_stalled',
                'severity': 'error', 'evidence': {'action_id': 'action-1'},
                'expected_progress': {'kind': 'publication_confirmed'},
            }], self.when.isoformat())
            self.incident_id = changes[0]['incident']['incident_id']

    def set_wrapper(self, code, **overrides):
        path = self.root / 'fixture.py'
        path.write_text(code, encoding='utf-8')
        self.config['supervision']['diagnosis'] = {
            'command': [sys.executable, str(path)], 'environment_allowlist': ['MODEL_FIXTURE'],
            'timeout_seconds': 5, **overrides,
        }

    def poll(self, *statuses, timeout=8, now=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.diagnose.poll(self.config, now=now or self.when)
            if result['status'] in statuses:
                return result
            time.sleep(.02)
        self.fail(f'Diagnosis did not reach {statuses}. Last result: {result!r}')

    def metadata(self):
        with closing(sqlite3.connect(self.db)) as conn:
            checkpoint = supervision_store.get_checkpoint(conn, 'diagnosis', self.incident_id, 'analysis')
        return checkpoint['metadata']

    def test_default_off_and_profile_rejects_unsafe_environment_and_command(self):
        self.assertEqual(self.diagnose.poll(self.config)['status'], 'disabled')
        self.assertIsNone(self.diagnose.normalize_diagnosis(None))
        for profile in (
            {'command': ['python3', '-c', 'pass']},
            {'command': 'python3 -c pass'},
            {'command': [sys.executable], 'environment_allowlist': ['GH_TOKEN']},
            {'command': [sys.executable], 'environment_allowlist': ['PYTHONPATH']},
            {'command': [sys.executable], 'timeout_seconds': 0},
        ):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                self.diagnose.normalize_diagnosis(profile)

    def test_real_wrapper_receives_only_scoped_redacted_input_and_minimal_environment(self):
        self.set_wrapper(VALID_WRAPPER)
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'test-business-marker', 'CODEX_HOME': '/test/secret',
                                          'ROBERT_CONFIG': '/test/production', 'MODEL_FIXTURE': 'allowed-for-test'}):
            self.assertEqual(self.diagnose.poll(self.config, now=self.when)['status'], 'launched')
            self.poll('succeeded')
        recorded = self.metadata()
        self.assertEqual(recorded['diagnosis']['recommended_action'], 'reconcile')
        self.assertEqual(recorded['diagnosis']['expected_progress'], 'Observe a matching receipt')
        with closing(sqlite3.connect(self.db)) as conn:
            status, evidence = conn.execute('SELECT status,evidence_json FROM supervision_incidents WHERE incident_id=?', (self.incident_id,)).fetchone()
        self.assertEqual(status, 'open')
        self.assertEqual(json.loads(evidence), {'action_id': 'action-1'})

    def test_rejects_unbound_or_unrecognized_recommendations(self):
        variants = (
            VALID_WRAPPER.replace("'reconcile'", "'run_shell'"),
            VALID_WRAPPER.replace("[bundle['evidence'][0]['id']]", "['invented-evidence']"),
            VALID_WRAPPER.replace("'bundle_sha256': digest", "'bundle_sha256': 'wrong'"),
        )
        for index, wrapper in enumerate(variants):
            with self.subTest(index=index), mock.patch.dict(os.environ, {'MODEL_FIXTURE': 'allowed-for-test'}):
                self.set_wrapper(wrapper, cooldown_seconds=1)
                self.poll('invalid_output', now=self.when + timedelta(seconds=index * 10))
                self.assertNotIn('diagnosis', self.metadata())

    def test_invalid_unicode_model_output_does_not_crash_observer(self):
        self.set_wrapper(VALID_WRAPPER.replace("'Observe a matching receipt'", 'chr(0xd800)'))
        with mock.patch.dict(os.environ, {'MODEL_FIXTURE': 'allowed-for-test'}):
            self.poll('invalid_output')
        self.assertEqual(self.metadata()['status'], 'invalid_output')
        with closing(sqlite3.connect(self.db)) as conn:
            runner = supervision_store.get_checkpoint(conn, 'diagnostic', 'installation', 'runner')['metadata']
        self.assertIsNone(runner['claim'])

    def test_reaped_short_process_completion_retries_after_database_contention(self):
        self.set_wrapper(VALID_WRAPPER)
        original = self.diagnose._complete
        calls = []
        def complete(*args, **kwargs):
            calls.append(args[1]['job_id'])
            if len(calls) == 1:
                raise sqlite3.OperationalError('database is locked')
            return original(*args, **kwargs)
        with mock.patch.dict(os.environ, {'MODEL_FIXTURE': 'allowed-for-test'}), \
                mock.patch.object(self.diagnose, '_stable_identity', return_value=None), \
                mock.patch.object(self.diagnose, '_complete', side_effect=complete):
            first = self.poll('completion_pending', 'unavailable', 'process_attention')
            self.assertEqual(first['status'], 'completion_pending')
            self.poll('succeeded')
        self.assertEqual(len(set(calls)), 1)
        self.assertEqual(self.metadata()['status'], 'succeeded')
        with closing(sqlite3.connect(self.db)) as conn:
            runner = supervision_store.get_checkpoint(conn, 'diagnostic', 'installation', 'runner')['metadata']
        self.assertIsNone(runner['claim'])
        self.assertEqual(len(runner['hourly_calls']), 1)

    def test_prelaunch_failure_completion_survives_database_contention(self):
        original = self.diagnose._complete
        for index, target in enumerate(('tempfile.TemporaryDirectory', 'build_worker_launch')):
            with self.subTest(target=target):
                self.set_wrapper(VALID_WRAPPER, cooldown_seconds=1)
                calls = []
                def complete(*args, **kwargs):
                    calls.append(args[1]['job_id'])
                    if len(calls) == 1:
                        raise sqlite3.OperationalError('database is locked')
                    return original(*args, **kwargs)
                when = self.when + timedelta(seconds=index * 10)
                with mock.patch('robert_agent.supervision.diagnosis.' + target, side_effect=OSError('fixture unavailable')), \
                        mock.patch.object(self.diagnose, '_complete', side_effect=complete):
                    result = self.poll('completion_pending', 'unavailable', now=when)
                    self.assertEqual(result['status'], 'completion_pending')
                    self.poll('launch_failed', now=when)
                self.assertEqual(len(set(calls)), 1)
                with closing(sqlite3.connect(self.db)) as conn:
                    runner = supervision_store.get_checkpoint(conn, 'diagnostic', 'installation', 'runner')['metadata']
                self.assertIsNone(runner['claim'])
                self.assertEqual(len(runner['hourly_calls']), index + 1)

    def test_oversized_utf8_evidence_is_checkpointed_without_blocking_same_lineage(self):
        self.set_wrapper('''import json,sys
raw = sys.stdin.buffer.read()
assert len(raw) <= 49152
p = json.loads(raw)
assert p['scope']['scope_type'] == 'controller'
json.dump({'bundle_id':p['bundle_id'],'bundle_sha256':p['bundle_sha256'],
           'diagnosis_class':'expected_wait','evidence_ids':[p['evidence'][0]['id']],
           'recommended_action':'wait','expected_progress':'Observe the next tick'},sys.stdout)
''', cooldown_seconds=60, max_calls_per_hour=1)
        text = '测' * 1800
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE supervision_incidents SET evidence_json=? WHERE incident_id=?",
                         (json.dumps({f'k{i}': text for i in range(6)}, ensure_ascii=False), self.incident_id))
            conn.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES ('a','target',1,'running')")
            conn.executemany(
                """INSERT INTO worker_phases(phase_id,attempt_id,phase,status,summary,created_at)
                   VALUES (?,'a','execute','running',?,?)""",
                [(f'p{i}', text, self.when.isoformat()) for i in range(3)])
        self.assertEqual(self.diagnose.poll(self.config, now=self.when),
                         {'ok': False, 'status': 'evidence_too_large', 'incident_id': self.incident_id})
        with closing(sqlite3.connect(self.db)) as conn, conn:
            small = supervision_store.apply_observations(conn, [{
                'scope_type': 'controller', 'scope_id': 'daemon', 'rule_id': 'controller_or_notice_stalled',
                'lineage_key': 'lineage', 'classification': 'confirmed',
                'reason_code': 'controller_progress_stalled', 'severity': 'error',
                'evidence': {'note': 'small'}, 'expected_progress': {'kind': 'controller_tick'},
            }], (self.when + timedelta(seconds=1)).isoformat())[0]['incident']['incident_id']
        restarted = subprocess.run([sys.executable, '-c', '''
import json,sys,time
from robert_agent.supervision import diagnosis as supervision_diagnose
config = json.loads(sys.argv[1])
while True:
    result = supervision_diagnose.poll(config, now=sys.argv[2])
    if result['status'] not in {'launched', 'running', 'completion_pending'}:
        print(json.dumps(result))
        break
    time.sleep(.02)
''', json.dumps(self.config), (self.when + timedelta(seconds=2)).isoformat()],
            text=True, capture_output=True, check=True, timeout=8)
        self.assertEqual(json.loads(restarted.stdout),
                         {'ok': True, 'status': 'succeeded', 'incident_id': small})
        record = self.metadata()
        self.assertEqual(record['status'], 'evidence_too_large')
        self.assertEqual(datetime.fromisoformat(record['evidence_retry_at']), self.when + timedelta(seconds=60))
        self.assertGreater(record['bundle_bytes'], record['byte_limit'])
        self.assertLess(len(json.dumps(record)), 512)
        with closing(sqlite3.connect(self.db)) as conn:
            runner = supervision_store.get_checkpoint(conn, 'diagnostic', 'installation', 'runner')['metadata']
        self.assertEqual(len(runner['hourly_calls']), 1)
        self.assertIsNone(runner['claim'])

    def test_oversized_incident_cannot_starve_suspect_candidate_at_cooldown_expiry(self):
        self.set_wrapper("""import json,sys
p=json.load(sys.stdin)
json.dump({'bundle_id':p['bundle_id'],'bundle_sha256':p['bundle_sha256'],
 'diagnosis_class':'expected_wait','evidence_ids':[p['evidence'][0]['id']],
 'recommended_action':'wait','expected_progress':'Observe worker progress'},sys.stdout)
""", cooldown_seconds=60)
        self.config['supervision']['poll_seconds'] = 60
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE supervision_incidents SET scope_type='controller',evidence_json=?,expected_progress_json=? WHERE incident_id=?",
                (json.dumps({f'e{i}': '𠀀' * 1800 for i in range(6)}, ensure_ascii=False),
                 json.dumps({f'p{i}': '𠀀' * 700 for i in range(4)}, ensure_ascii=False), self.incident_id))
        self.assertEqual(self.diagnose.poll(self.config, now=self.when)['status'], 'evidence_too_large')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES ('small','other','running',?,?)", [self.when.isoformat()] * 2)
            supervision_store.apply_observations(conn, [{
                'scope_type': 'task', 'scope_id': 'small', 'rule_id': 'worker_progress_stalled',
                'lineage_key': 'lineage', 'classification': 'suspect', 'severity': 'warning',
                'reason_code': 'progress_stalled', 'evidence': {'note': 'small'},
                'expected_progress': {'kind': 'worker_progress'},
            }], self.when.isoformat())
        later = self.when + timedelta(seconds=60)
        self.assertEqual(self.diagnose.poll(self.config, now=later)['status'], 'launched')
        result = self.poll('succeeded', now=later)
        self.assertEqual(result['candidate_id'], 'candidate:small:worker_progress_stalled')

    def test_provider_429_blocks_other_targets_and_survives_restart(self):
        wrapper = """import json,sys
p=json.load(sys.stdin)
json.dump({'bundle_id':p['bundle_id'],'bundle_sha256':p['bundle_sha256'],
           'error':{'code':'rate_limited','retry_after_seconds':120}},sys.stdout)
sys.exit(75)
"""
        self.set_wrapper(wrapper, cooldown_seconds=1, max_calls_per_hour=2)
        reply = self.poll('provider_rate_limited', 'exited')
        self.assertEqual(reply['status'], 'provider_rate_limited')
        self.assertNotIn('diagnosis', self.metadata())
        with closing(sqlite3.connect(self.db)) as conn, conn:
            supervision_store.apply_observations(conn, [{
                'scope_type':'controller','scope_id':'other','rule_id':'controller_or_notice_stalled',
                'classification':'confirmed','reason_code':'controller_progress_stalled','severity':'error',
                'evidence':{},'expected_progress':{'kind':'controller_tick'},
            }], self.when.isoformat())
        later = self.when + timedelta(seconds=5)
        self.assertEqual(self.diagnose.poll(self.config, now=later)['status'], 'provider_cooldown')
        restarted = subprocess.run([sys.executable, '-c',
            'import json,sys; from robert_agent.supervision import diagnosis as supervision_diagnose; '
            'print(json.dumps(supervision_diagnose.poll(json.loads(sys.argv[1]),now=sys.argv[2])))',
            json.dumps(self.config), later.isoformat()], text=True, capture_output=True, check=True, timeout=5)
        self.assertEqual(json.loads(restarted.stdout)['status'], 'provider_cooldown')
        self.poll('provider_rate_limited', now=self.when+timedelta(seconds=121))
        self.assertEqual(self.diagnose.poll(self.config, now=self.when+timedelta(seconds=243))['status'], 'rate_limited')
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM supervision_recovery_actions').fetchone()[0], 0)
            runner=supervision_store.get_checkpoint(conn,'diagnostic','installation','runner')['metadata']
            self.assertEqual(len(runner['hourly_calls']), 2)
            self.assertIsNone(runner['claim'])

    def test_provider_cooldown_includes_delay_before_thread_execution(self):
        self.set_wrapper("import json,sys; p=json.load(sys.stdin); "
            "json.dump({'bundle_id':p['bundle_id'],'bundle_sha256':p['bundle_sha256'],"
            "'error':{'code':'rate_limited','retry_after_seconds':1}},sys.stdout); sys.exit(75)",
            cooldown_seconds=1)
        original = self.diagnose._run
        started = []
        def delayed(*args, **kwargs):
            time.sleep(1.2)
            started.append(datetime.now(timezone.utc))
            return original(*args, **kwargs)
        with mock.patch.object(self.diagnose, '_run', side_effect=delayed):
            self.assertEqual(self.diagnose.poll(self.config)['status'], 'launched')
            self.poll('provider_rate_limited')
        retry_at = datetime.fromisoformat(self.metadata()['provider_error']['retry_at'])
        self.assertGreaterEqual(retry_at, started[0] + timedelta(seconds=1))
        self.assertEqual(self.diagnose.poll(self.config)['status'], 'provider_cooldown')

    def test_provider_error_requires_bound_strict_transport_evidence(self):
        variants = ({'code':'rate_limited','retry_after_seconds':True},
                    {'code':'rate_limited','retry_after_seconds':-1},
                    {'code':'rate_limited','retry_after_seconds':'120'},
                    {'code':'rate_limited','retry_after_seconds':86401},
                    {'code':'rate_limited','message':'secret'}, {'code':'arbitrary'})
        for index, error in enumerate(variants):
            with self.subTest(error=error):
                self.set_wrapper('import json,sys; p=json.load(sys.stdin); '
                    + "json.dump({'bundle_id':p['bundle_id'],'bundle_sha256':p['bundle_sha256'],'error':"
                    + repr(error) + '},sys.stdout); sys.exit(75)', cooldown_seconds=1)
                self.poll('invalid_output', now=self.when+timedelta(seconds=index*10))
        with closing(sqlite3.connect(self.db)) as conn:
            runner=supervision_store.get_checkpoint(conn,'diagnostic','installation','runner')['metadata']
            self.assertNotIn('provider_not_before', runner)

    def test_timeout_reaps_process_and_cooldown_and_hourly_budget_persist(self):
        self.set_wrapper('import time; time.sleep(30)', timeout_seconds=1, cooldown_seconds=2, max_calls_per_hour=1)
        self.poll('timed_out')
        self.assertEqual(self.metadata()['status'], 'timed_out')
        self.diagnose.shutdown(self.config)
        self.assertEqual(self.diagnose.poll(self.config, now=self.when + timedelta(seconds=5))['status'], 'rate_limited')

    def test_restart_observes_existing_group_before_starting_a_second_wrapper(self):
        self.set_wrapper('import time; time.sleep(30)', timeout_seconds=30)
        # Exit a separate caller without running its shutdown hook. The fixture
        # wrapper is its only child, and the new caller must honor its claim.
        launcher = '''
import json, os, sqlite3, sys, time
from robert_agent.supervision import diagnosis as supervision_diagnose; from robert_agent.supervision import store as supervision_store
cfg = json.loads(sys.argv[1])
assert supervision_diagnose.poll(cfg, now=sys.argv[2])['status'] == 'launched'
deadline = time.monotonic() + 5
while time.monotonic() < deadline:
    with sqlite3.connect(cfg['db_path']) as conn:
        state = supervision_store.get_checkpoint(conn, 'diagnostic', 'installation', 'runner')['metadata']
        identity = state['claim'].get('process_identity')
    if identity:
        print(json.dumps(identity), flush=True)
        os._exit(0)
    time.sleep(.02)
raise RuntimeError('missing persisted child identity')
'''
        started = subprocess.run([sys.executable, '-c', launcher, json.dumps(self.config), self.when.isoformat()],
                                 text=True, capture_output=True, check=True, timeout=8)
        identity = json.loads(started.stdout)
        self.addCleanup(worker_identity.terminate_recorded_identity, identity)
        self.poll('previous_process_running')
        result = self.poll('timed_out', now=self.when + timedelta(seconds=31))
        self.assertEqual(result['incident_id'], self.incident_id)
        self.assertFalse(worker_identity.live_process_group(identity['process_group_id']))
        self.assertEqual(self.diagnose.poll(self.config, now=self.when + timedelta(seconds=32))['status'], 'cooldown')
