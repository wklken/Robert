"""Recover native launch reservations only when the persisted outcome proves exit."""
import json
import unittest

from robert_agent import model_execution, work_items
from tests import test_review_coordinator as review_fixture
from tests import test_vote_coordinator as vote_fixture


FAILED_WITHOUT_PROCESS = {'ok': False, 'status': 'failed_dispatch', 'pid': None,
                          'error_type': 'FileNotFoundError'}


class NativeLaunchSlotRecoveryTests(unittest.TestCase):
    def fixture(self, kind):
        fixture = (review_fixture.ReviewCoordinatorTests() if kind == 'review'
                   else vote_fixture.VoteCoordinatorTests())
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        if kind == 'review':
            fixture.cycle([fixture.event()])
            driver = fixture.coordinator()
        else:
            driver = fixture.start()
        driver.prepare()
        with driver.store.connect(driver.db_path) as conn:
            conn.execute('UPDATE review_process_pool SET max_concurrency=1')
        return fixture, driver

    def new_run(self, driver):
        worker = driver.request['workers'][0]
        with driver.store.connect(driver.db_path) as conn:
            run = driver.store.new_run(conn, driver.round_id, driver.voter_stage, worker['name'],
                                       driver.attempt_id, driver.manifest['artifact_root'],
                                       driver.manifest['prompt_sha256'])
            driver.store.update_run(conn, run['run_id'], status='waiting')
        return run

    def failed_launch(self, driver):
        run = self.new_run(driver)
        driver.launcher = lambda **kwargs: dict(FAILED_WITHOUT_PROCESS)
        launched = driver.launch_with_slot(run, driver.request['workers'][0],
                                           driver.manifest['prompt_path'], {})
        self.assertEqual(launched, FAILED_WITHOUT_PROCESS)
        with driver.store.connect(driver.db_path) as conn:
            self.assertEqual(driver.store.process_pool_status(conn), {'limit': 1, 'in_use': 1})
        # Crash after launch outcome commits, before run_phase's release_slot.
        return run

    def cancel(self, driver):
        with driver.store.connect(driver.db_path) as conn:
            item = dict(conn.execute('SELECT * FROM work_items').fetchone())
        context = work_items.CommandContext('operator', 'maintainer', frozenset({item['repo_id']}),
                                            frozenset({'codex', 'claude'}))
        return work_items.execute_command(driver.db_path, item['work_item_id'], context=context,
                                          command='cancel', expected_version=item['version'],
                                          idempotency_key='cancel-slot', notifier=lambda _: None)

    def assert_released(self, driver, run):
        with driver.store.connect(driver.db_path) as conn:
            current = next(row for row in driver.store.runs(conn, driver.round_id)
                           if row['run_id'] == run['run_id'])
            self.assertEqual(current['status'], 'failed')
            self.assertEqual(current['dispatch'], FAILED_WITHOUT_PROCESS)
            self.assertFalse(current['slot_reserved'])
            self.assertEqual(driver.store.process_pool_status(conn), {'limit': 1, 'in_use': 0})
        waiting = self.new_run(driver)
        with driver.store.connect(driver.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            self.assertTrue(driver.store.reserve_slot(conn, waiting['run_id']))

    def test_restart_reclaims_failed_launch_slot_for_both_native_workflows(self):
        for kind in ('review', 'vote'):
            with self.subTest(kind=kind):
                fixture, driver = self.fixture(kind)
                run = self.failed_launch(driver)
                restarted = fixture.coordinator() if kind == 'review' else fixture.driver()
                restarted.reclaim_finished_slots()
                self.assert_released(restarted, run)

    def test_real_cancel_reclaims_failed_launch_slot_for_both_native_workflows(self):
        for kind in ('review', 'vote'):
            with self.subTest(kind=kind):
                _, driver = self.fixture(kind)
                run = self.failed_launch(driver)
                response = self.cancel(driver)
                self.assertTrue(response['ok'], response)
                with driver.store.connect(driver.db_path) as conn:
                    self.assertEqual(conn.execute('SELECT lifecycle FROM tasks WHERE task_id=?',
                                                   (driver.task_id,)).fetchone()[0], 'canceled')
                self.assert_released(driver, run)

    def test_unknown_and_cleanup_pending_launches_keep_reservation(self):
        for kind in ('review', 'vote'):
            with self.subTest(kind=kind):
                _, driver = self.fixture(kind)
                run = self.new_run(driver)
                with driver.store.connect(driver.db_path) as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    self.assertTrue(driver.store.reserve_slot(conn, run['run_id']))
                for status, recorded in [
                    ('starting', {}), ('failed', {}),
                    ('failed', {**FAILED_WITHOUT_PROCESS, 'cleanup_pending': True}),
                    ('failed', {'pid': None}),
                    ('failed', {**FAILED_WITHOUT_PROCESS, 'ok': True}),
                    ('failed', {**FAILED_WITHOUT_PROCESS, 'pid': 0}),
                ]:
                    with self.subTest(status=status, recorded=recorded):
                        with driver.store.connect(driver.db_path) as conn:
                            driver.store.update_run(conn, run['run_id'], status=status,
                                                     dispatch_json=json.dumps(recorded))
                        driver.reclaim_finished_slots()
                        with driver.store.connect(driver.db_path) as conn:
                            self.assertEqual(driver.store.process_pool_status(conn), {'limit': 1, 'in_use': 1})
                            model_execution.terminate_children(conn, driver.task_id, store=driver.store)
                            self.assertEqual(driver.store.process_pool_status(conn), {'limit': 1, 'in_use': 1})
                with driver.store.connect(driver.db_path) as conn:
                    driver.store.update_run(conn, run['run_id'], status='starting', dispatch_json='{}')
                with self.assertRaises(work_items.WorkItemConflictError):
                    self.cancel(driver)
                with driver.store.connect(driver.db_path) as conn:
                    self.assertEqual(driver.store.process_pool_status(conn), {'limit': 1, 'in_use': 1})


if __name__ == '__main__':
    unittest.main()
