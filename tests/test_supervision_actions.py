from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import storage, supervision_actions as actions


NOW = '2026-11-06T10:00:00+00:00'
LATER = '2026-11-06T11:00:00+00:00'
RECHECK = '2026-11-06T11:01:00+00:00'
RECHECK_AGAIN = '2026-11-06T11:02:00+00:00'


class SupervisionActionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / 'state.db'
        storage.init_database(self.db)
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES('repo','example/repo','default','main','/tmp','/tmp')")
        self.task('task', 'lineage')
        self.config = {'repos': [{'full_name': 'example/repo'}], 'supervision': {'mode': 'recover_known', 'recovery': {
            'repositories': ['example/repo'], 'allowed_actions': ['spool_replay', 'audit_retry']}}}

    def task(self, task_id, lineage, status='running'):
        stream = 'stream-' + task_id
        self.conn.execute("INSERT INTO workstreams(workstream_id,repo_id,active_task_id,created_at,updated_at) VALUES(?,'repo',?,?,?)",
                          (stream, task_id, NOW, NOW))
        self.conn.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at,metadata_json) VALUES(?,?,'running',?,?,?)",
                          (task_id, stream, NOW, NOW, json.dumps({'supervision_lineage_key': lineage})))
        self.conn.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES(?,?,1,?)",
                          ('attempt-' + task_id, task_id, status))

    def observation(self, task_id='task'):
        return {'scope_type': 'task', 'scope_id': task_id, 'classification': 'suspect',
                'lineage_key': 'lineage', 'rule_id': 'result_not_consumed', 'reason_code': 'result_waiting_ingestion',
                'evidence': {'attempt_id': 'attempt-' + task_id, 'spool_fingerprint': 'fingerprint'},
                'expected_progress': {'kind': 'result_ingested', 'attempt_id': 'attempt-' + task_id}}

    def plan(self, *, config=None):
        return actions.plan(self.conn, observations=[self.observation()], now=NOW, config=config or self.config)

    def begin(self, *, config=None, now=NOW, generation='fingerprint'):
        return actions.begin(self.conn, task_id='task', kind='spool_replay', target_id='attempt-task',
                             generation=generation, now=now, config=config or self.config)

    def test_plan_reserves_once_without_mutating_business_state(self):
        first = self.plan()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]['status'], 'requested')
        self.assertEqual(self.plan(), [])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM worker_results').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT status FROM attempts').fetchone()[0], 'running')

    def test_expired_unclaimed_request_cannot_repeat_same_generation(self):
        self.assertEqual(len(self.plan()), 1)
        actions.verify(self.conn, observations=[], now=LATER)
        self.assertEqual(actions.plan(self.conn, observations=[self.observation()], now=LATER, config=self.config), [])
        self.assertEqual(self.conn.execute('SELECT status,budget_cost FROM supervision_recovery_actions').fetchall(), [('rejected', 0)])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notifications WHERE notification_type='supervision_recovery_attention'").fetchone()[0], 1)

    def test_policy_exact_repo_and_known_lineage_required(self):
        disabled = {'repos': self.config['repos'], 'supervision': {'mode': 'recover_known', 'recovery': {
            'repositories': ['example/other'], 'allowed_actions': ['spool_replay']}}}
        self.assertEqual(self.plan(config=disabled), [])
        self.conn.execute("UPDATE tasks SET metadata_json='{}'")
        self.assertEqual(self.plan(), [])

    def test_changed_spool_generation_rejects_before_claim(self):
        self.plan()
        self.assertEqual(self.begin(generation='replacement')['status'], 'blocked')
        self.assertEqual(self.conn.execute('SELECT status,budget_cost FROM supervision_recovery_actions').fetchone(), ('rejected', 0))

    def test_fence_blocks_duplicate_and_stale_finish(self):
        self.plan()
        result = self.begin()
        self.assertEqual(result['status'], 'claimed')
        claim = result['claim']
        self.assertEqual(self.begin()['status'], 'blocked')
        self.assertFalse(actions.finish(self.conn, {**claim, 'claim_generation': claim['claim_generation'] - 1},
                                       outcome='observing', receipt={}, now=NOW))
        self.assertTrue(actions.finish(self.conn, claim, outcome='observing', receipt={}, now=NOW))
        actions.verify(self.conn, observations=[], now=NOW)
        self.assertEqual(self.conn.execute('SELECT status FROM supervision_recovery_actions').fetchone()[0], 'observing')

    def test_expired_claim_keeps_budget_until_same_result_is_verified(self):
        self.plan()
        self.assertEqual(self.begin()['status'], 'claimed')
        self.conn.commit()  # Simulate losing the executing process after claiming.
        with closing(sqlite3.connect(self.db)) as restarted, restarted:
            actions.verify(restarted, observations=[], now=LATER)
        self.assertEqual(self.conn.execute('SELECT status,budget_cost FROM supervision_recovery_actions').fetchone(), ('unknown', 1))
        self.assertEqual(self.begin(config={'supervision': {'mode': 'observe'}}, now=LATER)['status'], 'blocked')
        self.conn.execute("INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json) VALUES('result','task','attempt-task','comment_analysis',?,?)",
                          (LATER, json.dumps({'submission_fingerprint': 'different'})))
        self.conn.execute("""INSERT INTO wakeups(wakeup_id,repo_id,task_id,attempt_id,result_id,reason,dedupe_key,created_at,updated_at,not_before_at)
            VALUES('wakeup','repo','task','attempt-task','result','worker_result_ready','key',?,?,?)""", (LATER, LATER, LATER))
        actions.verify(self.conn, observations=[], now=RECHECK)
        self.assertEqual(self.conn.execute('SELECT status FROM supervision_recovery_actions').fetchone()[0], 'unknown')
        self.conn.execute("UPDATE worker_results SET metadata_json=?", (json.dumps({'submission_fingerprint': 'fingerprint'}),))
        actions.verify(self.conn, observations=[], now=RECHECK_AGAIN)
        self.assertEqual(self.conn.execute('SELECT status,budget_cost FROM supervision_recovery_actions').fetchone(), ('succeeded', 1))

    def test_native_retry_consumes_same_lineage_stage_budget(self):
        for task_id in ('task', 'replacement'):
            if task_id != 'task':
                self.task(task_id, 'lineage', 'completed')
            self.conn.execute('UPDATE attempts SET status=? WHERE task_id=?', ('completed', task_id))
            self.conn.execute("INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json) VALUES(?,?,?,'comment_analysis',?,?)",
                              ('result-' + task_id, task_id, 'attempt-' + task_id, NOW,
                               json.dumps({'audit_observation': {'attempt_count': 1, 'retryable': True}})))
        first = actions.begin(self.conn, task_id='task', kind='audit_retry', target_id='result-task',
                              config=self.config, now=NOW, native_retry=True)
        self.assertEqual(first['status'], 'claimed')
        actions.finish(self.conn, first['claim'], outcome='failed', receipt={}, now=NOW)
        next_attempt = actions.begin(self.conn, task_id='replacement', kind='audit_retry', target_id='result-replacement',
                                     config=self.config, now=NOW, native_retry=True)
        self.assertEqual(next_attempt['status'], 'blocked')
        self.assertEqual(next_attempt['reason_code'], 'recovery_budget_exhausted')

    def test_cancellation_after_claim_prevents_later_application(self):
        self.plan()
        claim = self.begin()['claim']
        self.conn.execute("UPDATE tasks SET lifecycle='canceled' WHERE task_id='task'")
        self.assertFalse(actions.current(self.conn, claim, config=self.config, now=NOW, generation='fingerprint'))
