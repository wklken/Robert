from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests import test_pr_remediation_integration as health_fixture
from tests import test_reliability as result_fixture
from tests import test_review_coordinator as review_fixture
from tests import test_run_once as run_fixture
from robert_agent import dispatch, execution_store, review_store, run_once, worker_identity
from robert_agent.worker import result


class LifecycleRemediationTests(unittest.TestCase):
    def test_classification_handoff_survives_git_failure_with_bounded_retry(self):
        fixture = run_fixture.RunOnceDryRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        event = {'id': 'assign-handoff', 'number': 16, 'source_type': 'issue',
                 'event_type': 'assigned', 'actor_login': 'robert-bot',
                 'assignment_actor_login': 'wklken', 'assigned_to': 'robert-bot',
                 'authorization_lookup_complete': True,
                 'event_fingerprint': 'assigned:assign-handoff',
                 'body': 'remove the DCO check', 'intent': 'unclear',
                 'title': 'CI change', 'state': 'open'}
        fixture.fixture_path.write_text(json.dumps({'events': [event]}))
        self.assertTrue(run_once.run_once(fixture.config_path, fixture_path=fixture.fixture_path,
                                         dry_run=True, skip_external=True)['ok'])
        db = fixture.data_dir / 'dd.sqlite3'
        with closing(sqlite3.connect(db)) as conn:
            task, attempt = conn.execute('SELECT task_id,attempt_id FROM attempts').fetchone()
        payload = {'task_id': task, 'attempt_id': attempt, 'output_type': 'classification_result', 'submission': {'planned_github_actions': [], 'consumed_event_fingerprints': ['assigned:assign-handoff'], 'verification': [], 'handoff': 'Route to implementation', 'used_skills': [], 'recommended_route': 'new-pr', 'branch_slug': 'ci-change'}}
        self.assertTrue(result.record_result(db, payload)['ok'])
        for failure in range(1, 4):
            with mock.patch.object(run_once, '_prepare_worktree', side_effect=subprocess.CalledProcessError(
                    1, ['git', 'fetch'], stderr='connection reset by peer')):
                run_once.run_once(fixture.config_path, dry_run=True, skip_external=True)
            with closing(sqlite3.connect(db)) as conn:
                metadata = json.loads(conn.execute('SELECT metadata_json FROM tasks WHERE task_id=?', (task,)).fetchone()[0])
                handoff = metadata.get('classification_handoff', {})
                self.assertEqual(handoff.get('attempt_count'), failure)
                self.assertEqual(conn.execute('SELECT count(*) FROM tasks WHERE parent_task_id=?', (task,)).fetchone()[0], 0)
                if failure < 3:
                    due = handoff['next_retry_at']
                    self.assertFalse(run_once.has_orphaned_classification_result(conn, ['repo:example/backend'], handoff['failed_at']))
                    self.assertTrue(run_once.has_orphaned_classification_result(conn, ['repo:example/backend'], due))
                else:
                    self.assertFalse(handoff['retryable'])
                    self.assertFalse(run_once.has_orphaned_classification_result(conn, ['repo:example/backend'], '2070-01-01T00:00:00+00:00'))
            if failure < 3:
                # Advance the caller's clock to this durable retry deadline.
                clock = mock.patch.object(run_once, '_now', return_value=due)
                clock.start()
                self.addCleanup(clock.stop)

    def process(self):
        process = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(120)'],
            start_new_session=True,
        )
        self.addCleanup(self.stop_process, process)
        time.sleep(.1)
        return process, worker_identity.capture_process_identity(process.pid)

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        dispatch._BACKGROUND_PROCESSES.pop(process.pid, None)

    def running_fixture(self, *, age_minutes=0):
        fixture = result_fixture.DurableResultTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        process, identity = self.process()
        now = datetime.now(timezone.utc)
        old = (now - timedelta(minutes=age_minutes)).isoformat()
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET status='running',started_at=?,heartbeat_at=?,metadata_json=json_set(?, '$.result_contract', 'compact-v1')",
                (old, old, json.dumps({'dispatch': identity})),
            )
            task, repo = conn.execute('''SELECT t.task_id,w.repo_id FROM tasks t
                JOIN workstreams w ON w.workstream_id=t.workstream_id''').fetchone()
            execution_store.ensure_subject(conn, 'task', task, 'human_request', now.isoformat())
            conn.execute('''INSERT INTO worker_phases(
                phase_id,attempt_id,phase,status,summary,created_at)
                VALUES ('started','attempt-1','analyze','running','Analyzing',?)''', (old,))
        return fixture, process, repo, now.isoformat()

    def test_stale_live_worker_keeps_dispatch_capacity(self):
        fixture, process, repo, now = self.running_fixture(age_minutes=25)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            run_once._supervise_running_attempts(conn, repo, {}, now)
            self.assertIsNone(process.poll())
            self.assertEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'stale')
            self.assertEqual(run_once._running_attempt_count(conn, repo), 1)
            self.assertEqual(run_once._running_attempt_count_for_repos(conn, [repo]), 1)

    def test_received_result_retains_ownership_until_process_exit(self):
        fixture, process, repo, now = self.running_fixture()
        self.assertTrue(result.record_result(fixture.db, fixture.payload)['ok'])
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            self.assertEqual(run_once._running_attempt_count(conn, repo), 1)
            self.assertNotEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'completed')
        self.stop_process(process)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            run_once._supervise_running_attempts(conn, repo, {}, now)
            self.assertEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'completed')
            self.assertEqual(run_once._running_attempt_count(conn, repo), 0)

    def test_result_arriving_during_recovery_observation_is_not_failed(self):
        fixture, process, repo, now = self.running_fixture()
        self.stop_process(process)
        receipts = []

        def inspect(*args, **kwargs):
            receipts.append(result.record_result(fixture.db, fixture.payload))
            return {'status': 'no_progress', 'commands': [], 'progress_signals': []}

        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            with mock.patch.object(run_once, '_inspect_attempt_recovery_with_metadata', side_effect=inspect):
                run_once._supervise_running_attempts(conn, repo, {}, now)
            self.assertTrue(receipts[0]['ok'])
            self.assertEqual(receipts[0]['status'], 'recorded')
            self.assertNotEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'failed')
            run_once._supervise_running_attempts(conn, repo, {}, now)
            self.assertEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'completed')
            self.assertEqual(conn.execute('SELECT count(*) FROM attempts').fetchone()[0], 1)

    def test_result_arriving_during_timeout_cleanup_is_not_failed(self):
        fixture, process, repo, now = self.running_fixture(age_minutes=50)
        receipts = []

        def terminate(*args, **kwargs):
            args[0].commit()  # Match the real cleanup helper's unlocked I/O boundary.
            self.stop_process(process)
            receipts.append(result.record_result(fixture.db, fixture.payload))
            return {'status': 'already_dead'}

        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            with mock.patch.object(run_once, '_terminate_owned_attempt', side_effect=terminate):
                run_once._supervise_running_attempts(conn, repo, {}, now)
            self.assertTrue(receipts[0]['ok'])
            self.assertEqual(receipts[0]['status'], 'recorded')
            self.assertNotEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'failed')
            run_once._supervise_running_attempts(conn, repo, {}, now)
            self.assertEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'completed')
            self.assertEqual(conn.execute('SELECT count(*) FROM attempts').fetchone()[0], 1)

    def test_recovery_backfill_does_not_hold_writer_during_git_inspection(self):
        from pathlib import Path
        fixture, process, _repo, now = self.running_fixture()
        self.stop_process(process)
        directory = Path(fixture.db).parent / 'artifacts' / 'task-1'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'attempt-1.verify.command.json').write_text(json.dumps({
            'task_id': 'task-1', 'attempt_id': 'attempt-1', 'phase': 'verify',
            'status': 'completed', 'returncode': 0, 'finished_at': now,
            'snapshot_status': 'deferred', 'summary': 'Command exited 0',
        }))
        observed = []

        def inspect_git(_path):
            with closing(sqlite3.connect(fixture.db, timeout=.05)) as other, other:
                other.execute('BEGIN IMMEDIATE')
                observed.append(True)
            return {}

        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            with mock.patch.object(run_once, '_git_recovery_summary', side_effect=inspect_git):
                recovery = run_once._inspect_attempt_recovery_with_metadata(
                    conn, 'attempt-1', 'task-1', {}, directory)
            self.assertEqual(recovery['status'], 'recoverable_progress')
            self.assertEqual(conn.execute("SELECT count(*) FROM worker_phases WHERE summary='Command exited 0'").fetchone()[0], 1)
        self.assertEqual(observed, [True])

    def test_startup_command_inspection_releases_writer(self):
        fixture, _process, repo, now = self.running_fixture(age_minutes=10)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute('DELETE FROM worker_phases')
        observed = []

        def inspect_commands(*args, **kwargs):
            with closing(sqlite3.connect(fixture.db, timeout=.05)) as other, other:
                other.execute('BEGIN IMMEDIATE')
                observed.append(True)
            return [{'status': 'running'}]

        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            with mock.patch.object(run_once, '_attempt_command_records', side_effect=inspect_commands):
                run_once._supervise_running_attempts(conn, repo, {}, now)
            self.assertEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'running')
        self.assertEqual(observed, [True])

    def test_control_deadline_does_not_fail_recoverable_worker(self):
        from pathlib import Path
        fixture, process, repo, now = self.running_fixture()
        self.stop_process(process)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute('DELETE FROM worker_phases')
            conn.execute('UPDATE attempts SET worktree_path=?', (str(Path(fixture.db).parent),))
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            with mock.patch.dict(os.environ, {'_ROBERT_CONTROL_DEADLINE': '0'}):
                with self.assertRaises(subprocess.TimeoutExpired):
                    run_once._supervise_running_attempts(conn, repo, {}, now)
            self.assertEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'running')
            self.assertEqual(conn.execute('SELECT lifecycle FROM tasks').fetchone()[0], 'running')
            self.assertEqual(conn.execute('SELECT binding_status FROM execution_subjects').fetchone()[0], 'active')
            self.assertEqual(conn.execute('SELECT count(*) FROM attempts').fetchone()[0], 1)

    def test_fast_receipt_before_pid_registration_is_still_supervised(self):
        from robert_agent import loop_engine
        fixture = result_fixture.DurableResultTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute("UPDATE attempts SET status='prepared',started_at=NULL,heartbeat_at=NULL,metadata_json=json_set('{}', '$.result_contract', 'compact-v1')")
            task, repo = conn.execute('''SELECT t.task_id,w.repo_id FROM tasks t
                JOIN workstreams w ON w.workstream_id=t.workstream_id''').fetchone()
            execution_store.ensure_subject(conn, 'task', task, 'human_request', datetime.now(timezone.utc).isoformat())
        self.assertTrue(result.record_result(fixture.db, fixture.payload)['ok'])
        process, identity = self.process()
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            run_once._record_dispatch_result(conn, 'attempt-1', {**identity, 'ok': True, 'status': 'running'})
            self.assertEqual(run_once._running_attempt_count(conn, repo), 1)
        self.stop_process(process)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            self.assertTrue(loop_engine.has_runnable_local_work(conn, repo_ids=[repo]))

    def test_health_reads_release_writer_between_prs(self):
        fixture = health_fixture.PrRemediationIntegrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.repo['pr_automation']['ci']['enabled'] = False
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            conn.execute('''INSERT INTO github_sources(source_id,repo_id,source_key,
                source_type,number,title,state,author_login) VALUES ('source:pr2',
                'repo:example/backend','github:example/backend!14','pull_request',
                14,'Fix2','open','robert-bot')''')
            conn.execute('''INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,
                origin_workstream_id,lifecycle,created_at,updated_at) VALUES (
                'github:example/backend!14','repo:example/backend','source:pr2',
                'github:example/backend#1','completed','2026-07-27','2026-07-27')''')
        delegate = fixture.runner()
        observed = []

        def runner(args, **kwargs):
            with closing(sqlite3.connect(fixture.db_path, timeout=0)) as other, other:
                other.execute('BEGIN IMMEDIATE')
            observed.append(args[-1])
            return delegate([arg.replace('/pulls/14', '/pulls/13') for arg in args], **kwargs)

        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            decisions = run_once._collect_pr_remediation_decisions(
                conn, 'repo:example/backend', fixture.repo, runner,
                '2026-10-26T00:00:00+00:00',
            )
        self.assertEqual(len(decisions), 2)
        self.assertEqual(len(observed), 2)

    def test_remote_close_stops_registered_native_models(self):
        fixture = review_fixture.ReviewCoordinatorTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.cycle([fixture.event()])
        driver = fixture.coordinator()
        driver.prepare()
        model, identity = self.process()
        dispatch._BACKGROUND_PROCESSES[model.pid] = model
        with review_store.connect(fixture.db) as conn:
            run = review_store.new_run(conn, driver.round_id, 'review', 'codex',
                                       driver.attempt_id, driver.manifest['artifact_root'],
                                       driver.manifest['prompt_sha256'])
            review_store.update_run(conn, run['run_id'], status='running', slot_reserved=1,
                                    dispatch_json=json.dumps(identity))
        ready = fixture.root / 'coordinator-ready'
        program = '''import signal,sys,time
from pathlib import Path
from robert_agent.review_coordinator import Coordinator
driver=Coordinator(sys.argv[1],sys.argv[2],sys.argv[3])
signal.signal(signal.SIGTERM,lambda *_:driver.stopping.set())
def prepare():
    Path(sys.argv[4]).touch()
    while True:
        driver.check_active()
        time.sleep(.05)
driver.prepare=prepare
driver.run()
'''
        coordinator = subprocess.Popen(
            [sys.executable, '-B', '-c', program, str(fixture.db), driver.task_id,
             driver.attempt_id, str(ready)], start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self.stop_process, coordinator)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(ready.exists())
        top = worker_identity.capture_process_identity(coordinator.pid)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute('''UPDATE attempts SET status='running',started_at=?,
                heartbeat_at=?,metadata_json=? WHERE attempt_id=?''',
                (now, now, json.dumps({'dispatch': top}), driver.attempt_id))
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            repo, stream, source, key, kind, number = conn.execute('''SELECT w.repo_id,
                w.workstream_id,gs.source_id,gs.source_key,gs.source_type,gs.number
                FROM tasks t JOIN workstreams w ON w.workstream_id=t.workstream_id
                JOIN github_sources gs ON gs.source_id=w.primary_source_id
                WHERE t.task_id=?''', (driver.task_id,)).fetchone()
            closed = run_once._record_remote_terminal_state(
                conn, repo, stream, source, key, kind, number, driver.task_id,
                {'state': 'closed', 'closed_at': now}, 'canceled', 'remote_pr_closed', now,
            )
        self.assertTrue(closed)
        self.assertIsNotNone(model.poll(), 'remote close must stop the independent model group')
        with review_store.connect(fixture.db) as conn:
            self.assertEqual(review_store.process_pool_status(conn)['in_use'], 0)


if __name__ == '__main__':
    unittest.main()
