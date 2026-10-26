from collections import Counter
import json
from pathlib import Path
import unittest
import threading
from unittest.mock import patch

import yaml
from robert_agent import model_execution, vote_store, work_items, audit_result
from tests import test_vote_coordinator as fixture_module


class VoteRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.VoteCoordinatorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_sealed_ballots_survive_producer_late_write(self):
        driver = self.fixture.start()
        driver.prepare()
        records = driver.vote()
        with vote_store.connect(self.fixture.db) as conn:
            runs = [r for r in vote_store.runs(conn, driver.round_id) if r['stage'] == 'vote']
        for run in runs:
            Path(run['output_dir'], 'ballot.json').write_text('late producer garbage')
        self.assertTrue(driver.synthesize(records).is_file())

    def test_crash_after_seal_before_registration_reuses_ballot(self):
        driver = self.fixture.start()
        driver.prepare()
        register = driver.register
        def crash(run, artifact):
            if run['stage'] == 'vote':
                raise KeyboardInterrupt('crash before registration')
            return register(run, artifact)
        # Both isolated voters can finish; neither accepted file needs another model call.
        with patch.object(driver, 'register', side_effect=crash), self.assertRaises(KeyboardInterrupt):
            driver.vote()
        self.fixture.driver().run()
        counts = Counter((c[0], c[1]) for c in self.fixture.calls)
        self.assertEqual(counts[('vote', 'codex')], 1)
        self.assertEqual(counts[('vote', 'claude')], 1)

    def test_fallback_is_per_worker_and_preserves_voter_prompt(self):
        path = self.fixture.fixture.config_path
        config = yaml.safe_load(path.read_text())
        config['workers']['claude'].update(model='primary', fallback_model='fallback')
        path.write_text(yaml.safe_dump(config))
        self.fixture.start()
        original_wait = model_execution.StageCoordinator._wait
        def timeout(driver, run, launched, seconds):
            if run['stage'] == 'vote' and run['worker_id'] == 'claude':
                raise RuntimeError('review worker timed out')
            return original_wait(driver, run, launched, seconds)
        with patch.object(model_execution.StageCoordinator, '_wait', timeout):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, 'timed out'):
                    self.fixture.driver().run()
        self.fixture.driver().run()
        calls = [c for c in self.fixture.calls if c[0] == 'vote']
        self.assertEqual([c[3] for c in calls if c[1] == 'claude'], ['primary','primary','fallback'])
        self.assertEqual(len({c[2] for c in calls}), 1)

    def test_unknown_vote_launch_requires_explicit_operator_abandonment(self):
        driver = self.fixture.start()
        driver.prepare()
        driver.launcher = lambda **kwargs: (_ for _ in ()).throw(OSError('unknown spawn'))
        # Synchronize after each publication lock is released, so both unknown
        # launches are durable before either exception stops sibling waiters.
        failures = threading.Barrier(2)
        launch = driver.launch_with_slot
        def unknown_launch(*args, **kwargs):
            try:
                return launch(*args, **kwargs)
            except OSError:
                failures.wait(timeout=5)
                raise
        with patch.object(driver, 'launch_with_slot', side_effect=unknown_launch), self.assertRaises(model_execution.ReviewStopped):
            driver.run()
        with vote_store.connect(self.fixture.db) as conn:
            conn.execute("UPDATE attempts SET status='failed' WHERE attempt_id=?", (driver.attempt_id,))
            unknown = [r for r in vote_store.runs(conn, driver.round_id) if r['status'] == 'starting']
            self.assertEqual(len(unknown), 2)
            item = dict(conn.execute('SELECT * FROM work_items WHERE workstream_id=?', (driver.session['workstream_id'],)).fetchone())
        context = work_items.CommandContext('operator', 'maintainer', frozenset({item['repo_id']}), frozenset({'codex','claude'}))
        with self.assertRaises(work_items.WorkItemConflictError):
            work_items.execute_command(self.fixture.db, item['work_item_id'], context=context, command='cancel',
                expected_version=item['version'], idempotency_key='ordinary-cancel')
        with self.assertRaises(work_items.WorkItemConflictError):
            work_items.execute_command(self.fixture.db, item['work_item_id'], context=context, command='cancel',
                expected_version=item['version'], idempotency_key='partial-vote-abandon',
                abandon_review_run_ids=[unknown[0]['run_id']])
        work_items.execute_command(self.fixture.db, item['work_item_id'], context=context, command='cancel',
            expected_version=item['version'], idempotency_key='confirmed-vote-abandon',
            abandon_review_run_ids=[run['run_id'] for run in unknown])
        with vote_store.connect(self.fixture.db) as conn:
            self.assertEqual(vote_store.process_pool_status(conn)['in_use'], 0)
            self.assertEqual(vote_store.session(conn, task_id=driver.task_id)['status'], 'canceled')
            confirmed = {run['run_id'] for run in unknown}
            self.assertTrue(all(run['status'] == 'abandoned' for run in vote_store.runs(conn, driver.round_id) if run['run_id'] in confirmed))
        with self.assertRaises(model_execution.ReviewStopped):
            driver.check_active()

    def test_report_audit_rejects_scope_swap_and_unregistered_comment(self):
        driver, action, scope = self.fixture.publication()
        raw = json.loads(action['metadata_json'])
        payload = {'output_type': 'multi_agent_vote_report', 'consumed_event_fingerprints': ['trigger'],
                   'used_skills': [], 'planned_github_actions': [raw]}
        self.assertTrue(audit_result.audit_result(payload, ['comment'], action_scope=scope)['ok'])
        for changes in [{'target_url': 'https://github.com/example/backend/issues/43'}, {'vote_round_id': 'another'},
                        {'body': 'invented result <!-- robert-comment fake -->'}]:
            candidate = {**payload, 'planned_github_actions': [{**raw, **changes}]}
            self.assertFalse(audit_result.audit_result(candidate, ['comment'], action_scope=scope)['ok'])
        ordinary = dict(raw)
        ordinary.pop('vote_round_id')
        self.assertFalse(audit_result.audit_result({**payload, 'planned_github_actions': [ordinary]}, ['comment'], action_scope=scope)['ok'])
