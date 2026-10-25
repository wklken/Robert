from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import unittest
from unittest.mock import patch

from robert_agent import board, review_coordinator, review_store, web, work_items
from tests import test_review_coordinator as fixtures


class ReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReviewCoordinatorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.cycle([self.fixture.event()])

    def unknown_start(self):
        driver = self.fixture.coordinator()
        driver.prepare()
        driver.request['workers'] = driver.request['workers'][:1]
        driver.launcher = lambda **kwargs: (_ for _ in ()).throw(OSError('crash during spawn'))
        with self.assertRaises(review_coordinator.ReviewStopped):
            driver.run()
        with review_store.connect(self.fixture.db) as conn:
            # The supervisor has observed the coordinator's exit.
            conn.execute("UPDATE attempts SET status='failed' WHERE attempt_id=?", (driver.attempt_id,))
            run = next(run for run in review_store.runs(conn, driver.round_id) if run['status'] == 'starting')
            item = dict(conn.execute('SELECT * FROM work_items WHERE workstream_id=?',
                                     (driver.session['workstream_id'],)).fetchone())
        context = work_items.CommandContext('operator', 'maintainer', frozenset({item['repo_id']}),
                                            frozenset({'codex', 'claude'}))
        return driver, run, item, context

    def test_sigterm_during_prepare_registers_launch_before_stopping(self):
        self.signal_during_stage('prepare')

    def test_sigterm_during_synthesis_registers_launch_before_stopping(self):
        self.signal_during_stage('synthesis')

    def signal_during_stage(self, stage):
        driver = self.fixture.coordinator()
        def launch(**kwargs):
            result = self.fixture.launcher(**kwargs)
            if Path(kwargs['prompt_path']).name.startswith(stage + '-prompt'):
                os.kill(os.getpid(), signal.SIGTERM)
            return result
        driver.launcher = launch
        previous = signal.getsignal(signal.SIGTERM)
        try:
            with patch.object(review_coordinator, 'Coordinator', return_value=driver):
                with self.assertRaises(review_coordinator.ReviewStopped):
                    review_coordinator.main(['--db', str(self.fixture.db), '--task-id', driver.task_id,
                                             '--attempt-id', driver.attempt_id])
        finally:
            signal.signal(signal.SIGTERM, previous)
        self.assertTrue(driver.stopping.is_set())
        with review_store.connect(self.fixture.db) as conn:
            run = next(run for run in review_store.runs(conn, driver.round_id) if run['stage'] == stage)
            self.assertTrue(run['dispatch'])
            self.assertEqual(review_store.process_pool_status(conn)['in_use'], 0)

    def test_confirmed_abandon_cancels_round_releases_slot_and_allows_new_trigger(self):
        driver, run, item, context = self.unknown_start()
        for attempt in ('first', 'again'):
            with self.assertRaises(work_items.WorkItemConflictError):
                work_items.execute_command(self.fixture.db, item['work_item_id'], context=context,
                    command='cancel', expected_version=item['version'], idempotency_key=attempt)
        detail = board.get_work_item_detail(self.fixture.db, item['work_item_id'])
        unknown = next(r for task in detail['tasks'] for r in task['review']['runs'] if r['status'] == 'starting')
        self.assertEqual(unknown['run_id'], run['run_id'])
        self.assertTrue(unknown['can_abandon'])
        kwargs = dict(context=context, command='cancel', expected_version=item['version'],
                      idempotency_key='confirmed-abandon', abandon_review_run_ids=[run['run_id']])
        result = work_items.execute_command(self.fixture.db, item['work_item_id'], **kwargs)
        self.assertEqual(result, work_items.execute_command(self.fixture.db, item['work_item_id'], **kwargs))
        with review_store.connect(self.fixture.db) as conn:
            current = next(r for r in review_store.runs(conn, driver.round_id) if r['run_id'] == run['run_id'])
            self.assertEqual(current['status'], 'abandoned')
            self.assertEqual(review_store.process_pool_status(conn)['in_use'], 0)
            self.assertEqual(review_store.session(conn, task_id=driver.task_id)['status'], 'canceled')
            metadata = json.loads(conn.execute("SELECT metadata_json FROM work_item_events WHERE idempotency_key='confirmed-abandon'").fetchone()[0])
            self.assertEqual(metadata['command_result']['abandoned_review_run_ids'], [run['run_id']])
        with self.assertRaises(review_coordinator.ReviewStopped):
            driver.check_active()
        self.fixture.cycle([self.fixture.event('after-abandon')])
        with review_store.connect(self.fixture.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 2)

    def test_abandon_rejects_changed_or_unrelated_run_without_releasing_slot(self):
        driver, run, item, context = self.unknown_start()
        with review_store.connect(self.fixture.db) as conn:
            review_store.update_run(conn, run['run_id'], dispatch_json=json.dumps({'pid': 424242}))
        for selected in ([run['run_id']], ['another-round-run']):
            with self.subTest(selected=selected), self.assertRaises(work_items.WorkItemConflictError):
                work_items.execute_command(self.fixture.db, item['work_item_id'], context=context,
                    command='cancel', expected_version=item['version'], idempotency_key='stale-selection',
                    abandon_review_run_ids=selected)
        with review_store.connect(self.fixture.db) as conn:
            self.assertEqual(review_store.process_pool_status(conn)['in_use'], 1)
            self.assertEqual(review_store.session(conn, task_id=driver.task_id)['status'], 'attention')

    def test_abandon_still_requires_coordinator_termination(self):
        driver, run, item, context = self.unknown_start()
        with review_store.connect(self.fixture.db) as conn:
            conn.execute("UPDATE attempts SET status='running' WHERE attempt_id=?", (driver.attempt_id,))
        with patch('robert_agent.run_once._terminate_attempt_process', return_value={'status': 'attention'}):
            with self.assertRaisesRegex(work_items.WorkItemConflictError, 'Worker could not be stopped'):
                work_items.execute_command(self.fixture.db, item['work_item_id'], context=context,
                    command='cancel', expected_version=item['version'], idempotency_key='parent-alive',
                    abandon_review_run_ids=[run['run_id']])
        with review_store.connect(self.fixture.db) as conn:
            self.assertEqual(review_store.process_pool_status(conn)['in_use'], 1)

    def test_web_abandon_requires_csrf_and_records_operator(self):
        driver, run, item, _ = self.unknown_start()
        context = web.build_control_context_from_config(self.fixture.config_path, host='127.0.0.1',
                                                       port=8765, operator_identity='review-operator')
        headers = {'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765',
                   'Content-Type': 'application/json', 'X-Idempotency-Key': 'web-abandon'}
        payload = json.dumps({'command': 'cancel', 'expected_version': item['version'],
                              'abandon_review_run_ids': [run['run_id']]}).encode()
        route = f"/api/work-items/{item['work_item_id']}/commands"
        status, _, _ = web.handle_dashboard_post(route, payload, self.fixture.db,
                                                control_context=context, request_headers=headers)
        self.assertEqual(status, 403)
        status, _, response = web.handle_dashboard_post(route, payload, self.fixture.db,
            control_context=context, request_headers={**headers, 'X-Robert-CSRF-Token': context.csrf_token})
        self.assertEqual(status, 200, response)
        with review_store.connect(self.fixture.db) as conn:
            event = conn.execute("SELECT actor_identity,metadata_json FROM work_item_events WHERE idempotency_key='web-abandon'").fetchone()
            self.assertEqual(event[0], 'review-operator')
            self.assertEqual(json.loads(event[1])['command_result']['abandoned_review_run_ids'], [run['run_id']])

    def test_abandon_requires_valid_run_list_and_repo_authority(self):
        _, run, item, context = self.unknown_start()
        for selected in (True, 'all', [True], [run['run_id'], run['run_id']]):
            with self.subTest(selected=selected), self.assertRaises(work_items.WorkItemValidationError):
                work_items.execute_command(self.fixture.db, item['work_item_id'], context=context,
                    command='cancel', expected_version=item['version'], idempotency_key='invalid-confirmation',
                    abandon_review_run_ids=selected)
        with self.assertRaises(work_items.WorkItemNotFoundError):
            work_items.execute_command(self.fixture.db, item['work_item_id'],
                context=work_items.CommandContext('operator', 'other', frozenset(), frozenset()),
                command='cancel', expected_version=item['version'], idempotency_key='wrong-repo',
                abandon_review_run_ids=[run['run_id']])

    def test_error_cleanup_allows_other_writers_while_each_child_stops(self):
        driver = self.fixture.coordinator()
        driver.prepare()
        with review_store.connect(self.fixture.db) as conn:
            for i, worker in enumerate(driver.request['workers']):
                run = review_store.new_run(conn, driver.round_id, 'review', worker['name'], driver.attempt_id,
                                           driver.manifest['artifact_root'], driver.manifest['prompt_sha256'])
                review_store.update_run(conn, run['run_id'], status='running', slot_reserved=1,
                                        dispatch_json=json.dumps({'pid': 424240+i}))
        observed = []
        def terminate(recorded):
            with closing(sqlite3.connect(self.fixture.db, timeout=.05)) as other, other:
                other.execute('BEGIN IMMEDIATE')
                other.execute('UPDATE review_process_pool SET max_concurrency=max_concurrency')
            observed.append(recorded['pid'])
            return {'status': 'terminated'}
        with patch.object(driver, 'prepare', side_effect=RuntimeError('phase failed')), \
             patch.object(review_coordinator, '_group_finished', return_value=False), \
             patch.object(review_coordinator, '_wait_for_group_exit', return_value=True), \
             patch.object(review_coordinator.worker_identity, 'inspect_recorded_identity', return_value='alive'), \
             patch.object(review_coordinator.worker_identity, 'terminate_recorded_identity', side_effect=terminate):
            with self.assertRaisesRegex(RuntimeError, 'phase failed'):
                driver.run()
        self.assertEqual(observed, [424240, 424241])
        with review_store.connect(self.fixture.db) as conn:
            self.assertEqual(review_store.process_pool_status(conn)['in_use'], 0)
