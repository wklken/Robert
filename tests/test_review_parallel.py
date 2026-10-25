from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sys
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

import yaml

from robert_agent import dispatch, review_coordinator, review_store, worker_identity
from robert_agent.publication_lock import task_publication_lock
from tests import test_review_coordinator as coordinator_fixture


class ParallelReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = coordinator_fixture.ReviewCoordinatorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.gate = self.fixture.root / 'release-models'
        self.launched = []
        self.slot_observations = []
        self.exit_codes = {}
        self.mutex = threading.Lock()
        self.addCleanup(self.stop_processes)

    def stop_processes(self):
        self.gate.touch()
        for launched, output in self.launched:
            worker_identity.terminate_recorded_identity(launched)
            process = dispatch._BACKGROUND_PROCESSES.pop(launched.get('pid'), None)
            if process is not None:
                process.wait(timeout=5)

    def configure(self, capacity):
        config = yaml.safe_load(self.fixture.config_path.read_text())
        config['review_max_concurrency'] = capacity
        config['max_concurrency'] = 1
        self.fixture.config_path.write_text(yaml.safe_dump(config))

    def launcher(self, **kwargs):
        with review_store.connect(self.fixture.db) as conn:
            self.slot_observations.append((Path(kwargs['prompt_path']).name,
                                           review_store.process_pool_status(conn)['in_use']))
        result = self.fixture.launcher(**kwargs)
        if Path(kwargs['prompt_path']).name != 'reviewer-prompt.md':
            return result
        output = Path(kwargs['log_dir'])
        script = '''import pathlib,sys,time
output,gate=map(pathlib.Path,sys.argv[1:3])
(output/'started').write_text(str(time.monotonic()))
deadline=time.monotonic()+15
while not (gate.exists() and (output/'registered').exists()):
    if time.monotonic()>deadline:
        sys.exit(17)
    time.sleep(.02)
(output/'finished').write_text(str(time.monotonic()))
sys.exit(int(sys.argv[3]))
'''
        exit_code = self.exit_codes.get(kwargs['worker_agent_config']['name'], 0)
        kwargs['worker_agent_config'] = {'agent': 'command', 'command_argv': [
            sys.executable, '-c', script, str(output), str(self.gate), str(exit_code)]}
        launched = dispatch.dispatch_worker(**kwargs, startup_probe_seconds=.01)
        # Keep every child alive through identity capture, even when an earlier
        # worker has already opened the shared release gate.
        if launched.get('ok'):
            (output / 'registered').touch()
        with self.mutex:
            self.launched.append((launched, output))
        return launched

    def driver(self, number=42):
        if number != 42:
            git = self.fixture.git_fixture
            git.git(git.upstream, 'update-ref', f'refs/pull/{number}/head', git.head)
        self.fixture.cycle([self.fixture.event(str(number)) | {'number': number}])
        with review_store.connect(self.fixture.db) as conn:
            task, attempt = conn.execute('''SELECT rs.task_id,a.attempt_id FROM review_sessions rs
                JOIN attempts a ON a.task_id=rs.task_id WHERE rs.pr_number=?
                ORDER BY a.attempt_no DESC LIMIT 1''', (number,)).fetchone()
        driver = review_coordinator.Coordinator(self.fixture.db, task, attempt,
            fetch_pr=lambda repo, n: self.fixture.pr(repo, n) | {'number': n}, launcher=self.launcher,
            sandbox=lambda manifest, output, data, prompt, readable, **kwargs: ([], {'ROBERT_REVIEW_OUTPUT_DIR': str(output)}),
            poll_seconds=.02)
        driver.prepare()
        return driver

    def started(self):
        with self.mutex:
            return sum((output / 'started').exists() for _, output in self.launched)

    def wait_started(self, count, timeout=3):
        deadline = time.monotonic() + timeout
        while self.started() < count and time.monotonic() < deadline:
            time.sleep(.02)
        return self.started()

    def pool(self):
        with review_store.connect(self.fixture.db) as conn:
            return review_store.process_pool_status(conn)

    def set_limit(self, limit):
        with review_store.connect(self.fixture.db) as conn:
            review_store.configure_process_pool(conn, limit)

    def cancel(self, driver):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with task_publication_lock(self.fixture.db, driver.task_id) as locked:
                if locked:
                    with review_store.connect(self.fixture.db) as conn:
                        conn.execute("UPDATE tasks SET lifecycle='canceled' WHERE task_id=?", (driver.task_id,))
                        self.assertNotEqual(review_coordinator.terminate_children(conn, driver.task_id), 'attention')
                    return
            time.sleep(.02)
        self.fail('could not acquire cancellation lock')

    def test_same_round_reviewers_overlap_with_identical_prompt(self):
        self.configure(2)
        driver = self.driver()
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(driver.review)
            try:
                self.assertEqual(self.wait_started(2), 2, 'both reviewers must start before either finishes')
            finally:
                self.gate.touch()
            records = future.result(timeout=5)
        self.assertEqual([r['worker_id'] for r in records], ['codex', 'claude'])
        prompts = [call[2] for call in self.fixture.calls if call[0] == 'review']
        self.assertEqual(len(set(prompts)), 1)
        driver.synthesize(records)
        self.assertEqual(self.pool(), {'limit': 2, 'in_use': 0})
        self.assertTrue(all(count >= 1 for stage, count in self.slot_observations))
        self.assertTrue(any(stage.startswith('synthesis-prompt') for stage, count in self.slot_observations))
        with review_store.connect(self.fixture.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 1)

    def test_released_gate_keeps_child_alive_until_identity_capture(self):
        self.configure(1)
        driver = self.driver()
        driver.request['workers'] = driver.request['workers'][:1]
        self.gate.touch()
        capture = worker_identity.capture_process_identity

        def delayed_capture(pid):
            # A slow ps call must not race a fixture child that was released
            # before launch (for example on retry or after waiting for a slot).
            output = Path(self.fixture.calls[-1][4])
            deadline = time.monotonic() + 3
            while not (output / 'started').exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue((output / 'started').exists())
            time.sleep(.2)
            return capture(pid)

        with patch.object(worker_identity, 'capture_process_identity', side_effect=delayed_capture) as observed:
            records = driver.review()
        self.assertEqual(observed.call_count, 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(self.pool()['in_use'], 0)

    def test_global_capacity_is_shared_by_independent_rounds(self):
        self.configure(1)
        first, second = self.driver(), self.driver(43)
        with ThreadPoolExecutor(2) as executor:
            futures = [executor.submit(driver.review) for driver in (first, second)]
            try:
                self.assertEqual(self.wait_started(1), 1)
                time.sleep(.4)
                self.assertEqual(self.started(), 1, 'a second round must share the global model budget')
            finally:
                self.gate.touch()
            for future in futures:
                self.assertEqual(len(future.result(timeout=5)), 2)
        self.assertEqual(len(self.launched), 4)
        intervals = sorted((float((output / name).read_text()), change)
                           for _, output in self.launched for name, change in [('started', 1), ('finished', -1)])
        active = peak = 0
        for _, change in intervals:
            active += change
            peak = max(peak, active)
        self.assertEqual(peak, 1)

    def test_capacity_changes_apply_to_waiters_without_killing_existing_models(self):
        self.configure(1)
        driver = self.driver()
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(driver.review)
            try:
                self.assertEqual(self.wait_started(1), 1)
                self.assertEqual(self.pool(), {'limit': 1, 'in_use': 1})
                self.set_limit(2)
                self.assertEqual(self.wait_started(2), 2)
                self.set_limit(1)
                time.sleep(.1)
                self.assertEqual(self.pool(), {'limit': 1, 'in_use': 2})
                self.assertFalse(any((output / 'finished').exists() for _, output in self.launched))
            finally:
                self.gate.touch()
            self.assertEqual(len(future.result(timeout=5)), 2)
        self.assertEqual(self.pool()['in_use'], 0)

    def test_waiting_for_capacity_does_not_consume_worker_timeout(self):
        self.configure(1)
        first, second = self.driver(), self.driver(43)
        for worker in second.request['workers']:
            worker['timeout_seconds'] = 1
        with ThreadPoolExecutor(2) as executor:
            first_future = executor.submit(first.review)
            self.assertEqual(self.wait_started(1), 1)
            second_future = executor.submit(second.review)
            try:
                time.sleep(1.2)
                self.assertEqual(self.started(), 1)
                self.assertFalse(second_future.done())
            finally:
                self.gate.touch()
            self.assertEqual(len(first_future.result(timeout=5)), 2)
            self.assertEqual(len(second_future.result(timeout=5)), 2)
        self.assertEqual(self.pool()['in_use'], 0)

    def test_cancel_stops_running_child_and_never_starts_waiting_reviewer(self):
        self.configure(1)
        driver = self.driver()
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(driver.run)
            try:
                self.assertEqual(self.wait_started(1), 1)
                self.cancel(driver)
                with self.assertRaises(review_coordinator.ReviewStopped):
                    future.result(timeout=5)
            finally:
                self.gate.touch()
        self.assertEqual(len(self.launched), 1)
        self.assertTrue(all(review_coordinator._group_finished(run) for run, _ in self.launched))
        self.assertEqual(self.pool()['in_use'], 0)
        with review_store.connect(self.fixture.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM github_actions').fetchone()[0], 0)

    def test_restart_adopts_parallel_children_and_files_without_duplicate_launch(self):
        self.configure(2)
        first = self.driver()
        barrier = threading.Barrier(2)

        def crash(*args):
            barrier.wait(timeout=3)
            raise KeyboardInterrupt('coordinator crash after both PID registrations')

        with patch.object(first, '_wait', side_effect=crash), self.assertRaises(KeyboardInterrupt):
            first.review()
        self.assertEqual(self.pool()['in_use'], 2)
        restarted = review_coordinator.Coordinator(self.fixture.db, first.task_id, first.attempt_id,
            fetch_pr=self.fixture.pr, launcher=self.launcher, sandbox=first.sandbox, poll_seconds=.02)
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(restarted.run)
            try:
                time.sleep(.1)
                self.assertEqual(len(self.launched), 2)
                self.assertFalse(future.done())
            finally:
                self.gate.touch()
            self.assertTrue(future.result(timeout=5).is_file())
        self.assertEqual(len(self.launched), 2)
        self.assertEqual(self.pool()['in_use'], 0)

    def test_exited_orphan_slot_can_be_reclaimed_by_another_round(self):
        self.configure(1)
        first = self.driver()
        first.request['workers'] = first.request['workers'][:1]
        self.gate.touch()
        with patch.object(first, 'release_slot', side_effect=KeyboardInterrupt('before slot release')):
            with self.assertRaises(KeyboardInterrupt):
                first.review()
        self.assertEqual(self.pool()['in_use'], 1)
        second = self.driver(43)
        second.run()
        self.assertEqual(self.pool()['in_use'], 0)
        with review_store.connect(self.fixture.db) as conn:
            original = next(run for run in review_store.runs(conn, first.round_id) if run['stage'] == 'review')
            self.assertEqual(original['status'], 'running')  # still available for file adoption

    def test_ambiguous_start_keeps_global_slot_across_recovery(self):
        self.configure(1)
        driver = self.driver()
        driver.request['workers'] = driver.request['workers'][:1]
        driver.launcher = lambda **kwargs: (_ for _ in ()).throw(OSError('unknown spawn result'))
        with self.assertRaisesRegex(review_coordinator.ReviewStopped, 'unknown'):
            driver.run()
        self.assertEqual(self.pool()['in_use'], 1)
        driver = self.fixture.coordinator()
        with self.assertRaisesRegex(review_coordinator.ReviewStopped, 'unknown'):
            driver.run()
        driver.reclaim_finished_slots()
        self.assertEqual(self.pool()['in_use'], 1)
        self.assertEqual(self.launched, [])

    def test_retry_reclaims_only_failed_model_and_keeps_successful_sibling(self):
        self.configure(2)
        driver = self.driver()
        self.exit_codes['codex'] = 1
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(driver.run)
            try:
                self.assertEqual(self.wait_started(2), 2)
            finally:
                self.gate.touch()
            with self.assertRaises(RuntimeError):
                future.result(timeout=5)
        self.assertEqual(self.pool()['in_use'], 0)
        with review_store.connect(self.fixture.db) as conn:
            successful = next(run for run in review_store.runs(conn, driver.round_id)
                              if run['stage'] == 'review' and run['status'] == 'succeeded')
        self.assertEqual(successful['worker_id'], 'claude')
        self.exit_codes.clear()
        retried = review_coordinator.Coordinator(self.fixture.db, driver.task_id, driver.attempt_id,
            fetch_pr=self.fixture.pr, launcher=self.launcher, sandbox=driver.sandbox, poll_seconds=.02)
        retried.run()
        self.assertEqual(len(self.launched), 3)
        reviews = [call for call in self.fixture.calls if call[0] == 'review']
        self.assertEqual([call[1] for call in reviews].count('codex'), 2)
        self.assertEqual([call[1] for call in reviews].count('claude'), 1)
        self.assertEqual(len({call[2] for call in reviews}), 1)
        self.assertEqual(self.pool()['in_use'], 0)

    def test_migration_counts_existing_live_or_unknown_runs_without_resetting_limit(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            legacy = review_store.SCHEMA.replace('  slot_reserved INTEGER NOT NULL DEFAULT 0,\n', '').replace('  started_at TEXT,\n', '')
            conn.executescript(legacy)
            for state in ('starting', 'running', 'succeeded'):
                conn.execute('''INSERT INTO review_runs(run_id,round_id,stage,worker_id,attempt_no,owner_attempt_id,
                    status,output_dir,prompt_sha256,dispatch_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (state, 'round', 'review', state, 1, 'attempt', state, state, 'hash', json.dumps({}),
                     '2026-10-25T00:00:00+00:00', '2026-10-25T00:00:00+00:00'))
            review_store.initialize(conn)
            self.assertEqual(review_store.process_pool_status(conn), {'limit': 4, 'in_use': 2})
            review_store.configure_process_pool(conn, 1)
            review_store.initialize(conn)
            self.assertEqual(review_store.process_pool_status(conn), {'limit': 1, 'in_use': 2})
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_runs').fetchone()[0], 3)

    def test_status_reads_existing_round_before_pool_migration_without_inventing_capacity(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            legacy = review_store.SCHEMA.split('CREATE TABLE IF NOT EXISTS review_process_pool')[0]
            legacy = legacy.replace('  slot_reserved INTEGER NOT NULL DEFAULT 0,\n', '').replace('  started_at TEXT,\n', '')
            conn.executescript(legacy)
            conn.execute('''INSERT INTO review_sessions(round_id,repo,pr_number,workstream_id,trigger_fingerprint,
                task_id,request_json,created_at,updated_at) VALUES ('round','repo',1,'stream','trigger','task','{}','now','now')''')
            conn.execute('''INSERT INTO review_runs(run_id,round_id,stage,worker_id,attempt_no,owner_attempt_id,
                status,output_dir,prompt_sha256,created_at,updated_at)
                VALUES ('run','round','review','codex',1,'attempt','running','output','hash','now','now')''')
            self.assertIsNone(review_store.task_status(conn, 'task')['process_pool'])
            board = review_store.board_status(conn, 'task')
            self.assertIsNone(board['process_pool'])
            self.assertIsNone(board['runs'][0]['slot_reserved'])
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='review_process_pool'").fetchone())

    def test_slot_reservation_is_atomic_between_processes(self):
        import subprocess
        self.configure(1)
        driver = self.driver()
        with review_store.connect(self.fixture.db) as conn:
            runs = [review_store.new_run(conn, driver.round_id, 'review', worker, driver.attempt_id,
                    driver.manifest['artifact_root'], driver.manifest['prompt_sha256']) for worker in ('codex', 'claude')]
            for run in runs:
                review_store.update_run(conn, run['run_id'], status='waiting')
        script = '''import sys
from robert_agent import review_store
with review_store.connect(sys.argv[1]) as conn:
    conn.execute('BEGIN IMMEDIATE')
    acquired=review_store.reserve_slot(conn,sys.argv[2])
print(int(acquired))
'''
        processes = [subprocess.Popen([sys.executable, '-c', script, str(self.fixture.db), run['run_id']],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for run in runs]
        outputs = [process.communicate(timeout=5) for process in processes]
        self.assertTrue(all(process.returncode == 0 for process in processes), outputs)
        self.assertEqual(sorted(output.strip() for output, error in outputs), ['0', '1'])
        self.assertEqual(self.pool()['in_use'], 1)

    def test_cancel_during_thread_submission_wakes_already_submitted_waiter(self):
        driver = object.__new__(review_coordinator.Coordinator)
        driver.stopping = threading.Event()
        observed = []

        class InterruptedSubmission(ThreadPoolExecutor):
            def submit(self, *args, **kwargs):
                super().submit(*args, **kwargs)
                raise review_coordinator.ReviewStopped('termination during submission')

        with patch.object(review_coordinator, 'ThreadPoolExecutor', InterruptedSubmission):
            with self.assertRaisesRegex(review_coordinator.ReviewStopped, 'termination during submission'):
                driver.parallel([1, 2], lambda item: observed.append(driver.stopping.wait(.5)))
        self.assertEqual(observed, [True])
