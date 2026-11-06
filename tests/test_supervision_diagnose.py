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
from robert_agent import storage, supervision_store, worker_identity


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
        from robert_agent import supervision_diagnose

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
from robert_agent import supervision_diagnose, supervision_store
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
