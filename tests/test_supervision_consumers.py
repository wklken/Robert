from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import result_contract, result_spool


class SupervisionSpoolConsumerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / 'state.db'

    def save(self, attempt_id):
        fingerprint = f'fingerprint-{attempt_id}'
        with result_spool.locked_record(self.db, attempt_id) as path:
            result_spool.atomic_write(path, {
                'status': 'pending', 'normalizer_version': 1, 'result_contract': result_contract.CONTRACT_ID,
                'fingerprint': fingerprint, 'payload': {'attempt_id': attempt_id, 'task_id': f'task-{attempt_id}',
                    '_submission_fingerprint': fingerprint, '_result_contract': result_contract.CONTRACT_ID},
            })
        return path

    def test_exact_replay_never_ingests_an_unrelated_spool_file(self):
        exact = self.save('wanted')
        unrelated = self.save('other')
        seen = []

        def writer(_db, payload, **_kwargs):
            seen.append(payload['attempt_id'])
            return {'ok': True, 'status': 'recorded'}

        self.assertEqual(result_spool.replay(self.db, writer, attempt_id='wanted')['recorded'], 1)
        self.assertEqual(seen, ['wanted'])
        self.assertFalse(exact.exists())
        self.assertTrue(unrelated.exists())

    def test_recovery_fence_retains_the_spool_and_does_not_starve_other_attempts(self):
        first = self.save('first')
        second = self.save('second')
        (blocked, healthy) = sorted((first, second))

        def writer(_db, payload, **_kwargs):
            from hashlib import sha256
            current = sha256(payload['attempt_id'].encode()).hexdigest() + '.json'
            if current == blocked.name:
                return {'ok': True, 'status': 'replay_blocked'}
            return {'ok': True, 'status': 'recorded'}

        self.assertEqual(result_spool.replay(self.db, writer), {'recorded': 1, 'deferred': 1, 'rejected': 0})
        self.assertTrue(blocked.exists())
        self.assertFalse(healthy.exists())


class SupervisionConsumerIntegrationTests(unittest.TestCase):
    def fixture(self, test_class):
        fixture = test_class()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def config(self, config_path, kind):
        from robert_agent import validate_config
        config = validate_config.validate_config(config_path, skip_external=True)
        self.assertTrue(config['ok'], config)
        config.pop('config_path', None)  # Pass the trusted in-memory fixture policy directly.
        config['supervision'] = {'mode': 'recover_known', 'recovery': {
            'allowed_actions': [kind], 'repositories': [repo['full_name'] for repo in config['repos']],
        }}
        return config

    def request(self, conn, config, task_id, rule_id, reason_code, evidence, now):
        from robert_agent import supervision_actions, supervision_lineage
        lineage = supervision_lineage.get_task_lineage(conn, task_id)
        self.assertTrue(lineage)
        request, = supervision_actions.plan(conn, config=config, now=now, observations=[{
            'scope_type': 'task', 'scope_id': task_id, 'lineage_key': lineage,
            'classification': 'suspect', 'rule_id': rule_id, 'reason_code': reason_code,
            'evidence': evidence,
        }])
        return request['action_id']

    def test_spool_replay_commits_bound_result_wakeup_and_recovery_receipt(self):
        from tests import test_compact_result
        from robert_agent import run_once
        from robert_agent.worker import result
        fixture = self.fixture(test_compact_result.CompactResultTests)
        db, task_id, attempt_id, _, _ = fixture.prepare('comment')
        config = self.config(fixture.config_path, 'spool_replay')
        with patch.object(result, '_record_result_db', side_effect=sqlite3.OperationalError('database is locked')):
            saved = result.record_result(db, fixture.envelope(task_id, attempt_id))
        self.assertEqual(saved['status'], 'deferred')
        path, = result_spool.directory(db).glob('*.json')
        fingerprint = json.loads(path.read_text())['fingerprint']
        with closing(sqlite3.connect(db)) as conn, conn:
            action_id = self.request(conn, config, task_id, 'result_not_consumed', 'result_waiting_ingestion',
                                     {'attempt_id': attempt_id, 'spool_fingerprint': fingerprint}, run_once._now())
        self.assertEqual(result.replay_pending_results(db, config=config, attempt_id=attempt_id)['recorded'], 1)
        self.assertEqual(result.replay_pending_results(db, config=config, attempt_id=attempt_id)['recorded'], 0)
        with closing(sqlite3.connect(db)) as conn:
            result_id, = conn.execute('SELECT result_id FROM worker_results WHERE attempt_id=?', (attempt_id,)).fetchone()
            self.assertTrue(conn.execute('SELECT 1 FROM wakeups WHERE result_id=?', (result_id,)).fetchone())
            status, encoded = conn.execute('SELECT status,receipt_json FROM supervision_recovery_actions WHERE action_id=?',
                                           (action_id,)).fetchone()
            self.assertEqual(status, 'observing')
            self.assertEqual(json.loads(encoded), {'attempt_id': attempt_id, 'result_id': result_id, 'fingerprint': fingerprint})

    def test_audit_refuses_stale_request_when_operator_cancels_during_inspection(self):
        from tests import test_compact_result
        from robert_agent import run_once
        from robert_agent.worker import result
        fixture = self.fixture(test_compact_result.CompactResultTests)
        db, task_id, attempt_id, _, _ = fixture.prepare('comment')
        config = self.config(fixture.config_path, 'audit_retry')
        now = run_once._now()
        recorded = result.record_result(db, fixture.envelope(task_id, attempt_id))
        self.assertTrue(recorded['ok'], recorded)
        with closing(sqlite3.connect(db)) as conn, conn:
            repo_id, = conn.execute('SELECT repo_id FROM repos').fetchone()
            action_id = self.request(conn, config, task_id, 'result_not_consumed', 'result_waiting_audit',
                                     {'result_id': recorded['result_id']}, now)

        original = run_once.result_scope.for_result
        def cancel_during_inspection(*args, **kwargs):
            with closing(sqlite3.connect(db)) as writer, writer:
                writer.execute("UPDATE tasks SET lifecycle='canceled' WHERE task_id=?", (task_id,))
            return original(*args, **kwargs)

        with closing(sqlite3.connect(db)) as conn, conn, patch.object(run_once.result_scope, 'for_result', side_effect=cancel_during_inspection):
            self.assertEqual(run_once._audit_completed_results(conn, repo_id, fixture.data_dir, db, config['repos'][0],
                'test-recovery', now, False, [], {'remaining': 1}, config_result=config), 0)
            self.assertEqual(conn.execute('SELECT status,budget_cost FROM supervision_recovery_actions WHERE action_id=?',
                                          (action_id,)).fetchone(), ('rejected', 0))
            self.assertIsNone(conn.execute("SELECT json_extract(metadata_json,'$.audit') FROM worker_results").fetchone()[0])

    def test_unknown_audit_claim_does_not_keep_the_result_wakeup_runnable(self):
        from tests import test_compact_result
        from robert_agent import loop_engine, run_once, supervision_actions
        from robert_agent.worker import result
        fixture = self.fixture(test_compact_result.CompactResultTests)
        db, task_id, attempt_id, _, _ = fixture.prepare('comment')
        config = self.config(fixture.config_path, 'audit_retry')
        now = run_once._now()
        recorded = result.record_result(db, fixture.envelope(task_id, attempt_id))
        self.assertTrue(recorded['ok'], recorded)
        with closing(sqlite3.connect(db)) as conn, conn:
            self.request(conn, config, task_id, 'result_not_consumed', 'result_waiting_audit',
                         {'result_id': recorded['result_id']}, now)
            self.assertTrue(loop_engine.has_runnable_local_work(conn, config_result=config, now=now,
                                                               include_publication=False))
            gate = supervision_actions.begin(conn, task_id=task_id, kind='audit_retry', target_id=recorded['result_id'],
                                               now=now, config=config)
            self.assertEqual(gate['status'], 'claimed')
            self.assertTrue(supervision_actions.finish(conn, gate['claim'], outcome='unknown', receipt={}, now=now))
            self.assertFalse(loop_engine.has_runnable_local_work(conn, config_result=config, now=now,
                                                                include_publication=False))
            self.assertEqual(conn.execute("SELECT status FROM wakeups WHERE reason='worker_result_ready'").fetchone(), ('pending',))

    def test_handoff_uses_original_due_path_and_records_one_successor(self):
        from tests import test_pending_handoff_recovery
        from robert_agent import pending_handoff, run_once
        fixture = self.fixture(test_pending_handoff_recovery.PendingHandoffRecoveryTests)
        config = self.config(fixture.fixture.config_path, 'handoff_resume')
        now = run_once._now()
        repo_id = 'repo:example/backend'
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle='completed',metadata_json=json_set(metadata_json,'$.pending_handoff.status','pending')")
            conn.execute("UPDATE worker_results SET metadata_json=json_set(metadata_json,'$.audit.status','accepted')")
            conn.execute("UPDATE github_actions SET audit_status='accepted'")
            action_id = self.request(conn, config, fixture.task, 'handoff_stalled', 'handoff_waiting_consumer', {}, now)
            self.assertTrue(pending_handoff.ready_rows(conn, [repo_id], now, config=config))
        original = run_once._prepare_worktree
        with closing(sqlite3.connect(fixture.db)) as conn, conn, patch.object(run_once, '_prepare_worktree',
                side_effect=lambda repo, event, route, _dry_run: original(repo, event, route, True)):
            self.assertEqual(run_once._recover_pending_handoffs(conn, repo_id, fixture.fixture.data_dir, fixture.db,
                config['repos'][0], now, False, [], {'remaining': 1}, config), 1)
            child, = conn.execute('SELECT task_id FROM tasks WHERE parent_task_id=?', (fixture.task,)).fetchone()
            status, encoded = conn.execute('SELECT status,receipt_json FROM supervision_recovery_actions WHERE action_id=?',
                                           (action_id,)).fetchone()
            self.assertEqual(status, 'observing')
            self.assertEqual(json.loads(encoded)['child_task_id'], child)
            self.assertEqual(pending_handoff.ready_rows(conn, [repo_id], now, config=config), [])


if __name__ == '__main__':
    unittest.main()
