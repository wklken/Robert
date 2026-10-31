"""A stopped admission lane still bounds and reaps its existing workers."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tests import test_terminal_github_followup as fixtures
from robert_agent import daemon, daemon_state, run_once, validate_config, worker_identity


class PausedWorkerSupervisionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TerminalGithubFollowupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.deliver('first')
        self.db = self.fixture.db_path
        self.config_path = self.fixture.fixture.config_path
        self.config = validate_config.validate_config(self.config_path, skip_external=True)
        self.now = datetime.now(timezone.utc)
        with closing(daemon_state.connect(self.db)) as conn, conn:
            owner = daemon_state.start_daemon_run(conn, config_path=str(self.config_path), owner_id='owner', now=self.now.isoformat())['daemon_run_id']
        self.context = daemon.DaemonContext(str(self.config_path), None, self.db, self.fixture.fixture.repo_id,
            3, self.config['daemon'], owner, 'lease', repo_ids=[self.fixture.fixture.repo_id], runtime_config=self.config)
        self.context.next_live_poll_at = '2099-01-01T00:00:00+00:00'
        self.context.background_lanes = {name: SimpleNamespace(process=None, poll=mock.Mock(return_value=None), start=mock.Mock()) for name in ('scan', 'publish')}
        for _ in range(3):
            daemon._record_control_outcome(self.context, 'local', {'ok': False, 'status': 'child_failed'}, now=self.now.isoformat())

    def process(self, age=120):
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], start_new_session=True)
        self.addCleanup(self.stop, proc)
        time.sleep(.05)
        identity = worker_identity.capture_process_identity(proc.pid)
        metadata = {'result_contract': 'compact-v1', 'dispatch': {**identity, 'worker_launch': {'timeout_seconds': 60}}}
        old = (self.now - timedelta(seconds=age)).isoformat()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempts SET status='running',started_at=?,heartbeat_at=?,metadata_json=?", (old, old, json.dumps(metadata)))
        return proc

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=3)

    def test_real_worker_times_out_while_local_lane_stays_paused(self):
        proc = self.process()
        with mock.patch.object(daemon, '_maybe_prune'):
            response = daemon.run_once_decision(self.context, now=self.now.isoformat())
        self.assertIsNotNone(proc.poll(), response)
        self.assertIn('local', self.context.control_attention)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'failed')
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM leases WHERE resource_type='agent_run' AND status='active'").fetchone()[0], 0)
        self.context.background_lanes['scan'].start.assert_not_called()

    def test_supervision_only_keeps_healthy_worker_and_avoids_git_recovery(self):
        from robert_agent import worker_supervision
        proc = self.process(age=0)
        with mock.patch.object(run_once, '_prepare_worktree', side_effect=AssertionError('no preparation')), \
             mock.patch.object(run_once, '_inspect_attempt_recovery_with_metadata', side_effect=AssertionError('no Git recovery')):
            result = worker_supervision.run(self.config_path)
            self.assertTrue(result['ok'], result)
            self.assertIsNone(proc.poll())
            self.stop(proc)
            result = worker_supervision.run(self.config_path)
            self.assertTrue(result['ok'], result)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'failed')

    def test_supervision_only_respects_repository_lease(self):
        from robert_agent import worker_supervision
        proc = self.process()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            run_once._acquire_agent_lease(conn, self.fixture.fixture.repo_id, 'other-owner', self.now.isoformat(), 60)
        result = worker_supervision.run(self.config_path)
        self.assertTrue(result['ok'], result)
        self.assertIsNone(proc.poll())

    def test_received_result_is_reaped_without_audit_or_publication(self):
        from robert_agent import worker_supervision
        from robert_agent.worker import result
        proc = self.process(age=0)
        with closing(sqlite3.connect(self.db)) as conn:
            task, attempt = conn.execute('SELECT task_id,attempt_id FROM attempts').fetchone()
        receipt = result.record_result(self.db, {
            'task_id': task, 'attempt_id': attempt, 'output_type': 'comment_analysis',
            'submission': {'planned_github_actions': [{'type': 'comment', 'body': 'Analysis finished'}],
                'consumed_event_fingerprints': ['comment:first'], 'verification': [], 'handoff': 'done', 'used_skills': []}})
        self.assertTrue(receipt['ok'], receipt)
        self.stop(proc)
        self.assertTrue(worker_supervision.run(self.config_path)['ok'])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'completed')
            self.assertEqual(conn.execute('SELECT audit_status,publish_status FROM github_actions').fetchone(), ('pending', 'not_published'))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 1)

    def test_unknown_identity_keeps_capacity_and_does_not_signal(self):
        from robert_agent import worker_supervision
        proc = self.process()
        with mock.patch.object(run_once, '_dispatch_process_status', return_value={'status': 'unknown'}), \
             mock.patch.object(run_once, '_terminate_owned_attempt', side_effect=AssertionError('unverified identity')):
            self.assertTrue(worker_supervision.run(self.config_path)['ok'])
        self.assertIsNone(proc.poll())
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'running')
            self.assertEqual(conn.execute('SELECT binding_status FROM execution_subjects').fetchone()[0], 'attention')

    def test_busy_task_lock_does_not_prevent_other_worker_cleanup(self):
        from robert_agent import execution_store, worker_supervision
        from robert_agent.publication_lock import task_publication_lock
        proc = self.process()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            task = conn.execute('SELECT task_id FROM tasks').fetchone()[0]
            now = self.now.isoformat()
            conn.execute("INSERT INTO workstreams(workstream_id,repo_id,lifecycle,active_task_id,created_at,updated_at) VALUES('busy-stream',?,'active','busy-task',?,?)", (self.fixture.fixture.repo_id, now, now))
            conn.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES('busy-task','busy-stream','queued',?,?)", (now, now))
            execution_store.ensure_subject(conn, 'task', 'busy-task', 'human_request', now)
        with task_publication_lock(self.db, 'busy-task') as locked:
            self.assertTrue(locked)
            self.assertTrue(worker_supervision.run(self.config_path)['ok'])
        self.assertIsNotNone(proc.poll())
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT status FROM attempts WHERE task_id=?', (task,)).fetchone()[0], 'failed')

    def test_deferred_result_is_ingested_before_paused_exit_finalization(self):
        from robert_agent import worker_supervision, result_spool
        from robert_agent.worker import result
        proc = self.process(age=0)
        with closing(sqlite3.connect(self.db)) as conn:
            task, attempt = conn.execute('SELECT task_id,attempt_id FROM attempts').fetchone()
        payload = {'task_id': task, 'attempt_id': attempt, 'output_type': 'comment_analysis',
            'submission': {'planned_github_actions': [{'type': 'comment', 'body': 'Analysis finished'}],
                'consumed_event_fingerprints': ['comment:first'], 'verification': [], 'handoff': 'done', 'used_skills': []}}
        with mock.patch.object(result, '_record_result_db', side_effect=sqlite3.OperationalError('database is locked')):
            self.assertEqual(result.record_result(self.db, payload)['status'], 'deferred')
        self.stop(proc)
        self.assertTrue(worker_supervision.run(self.config_path)['ok'])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM worker_results').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'completed')
        self.assertFalse(list(result_spool.directory(self.db).glob('rejected/*.json')))

    def test_deferred_or_inflight_spool_prevents_failure_when_replay_is_busy(self):
        from robert_agent import worker_supervision, result_spool
        from robert_agent.worker import result
        proc = self.process(age=0)
        with closing(sqlite3.connect(self.db)) as conn:
            task, attempt = conn.execute('SELECT task_id,attempt_id FROM attempts').fetchone()
        self.stop(proc)
        with result_spool.locked_record(self.db, attempt):
            self.assertTrue(worker_supervision.run(self.config_path)['ok'])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'running')
        payload = {'task_id': task, 'attempt_id': attempt, 'output_type': 'comment_analysis',
            'submission': {'planned_github_actions': [{'type': 'comment', 'body': 'Analysis finished'}],
                'consumed_event_fingerprints': ['comment:first'], 'verification': [], 'handoff': 'done', 'used_skills': []}}
        with mock.patch.object(result, '_record_result_db', side_effect=sqlite3.OperationalError('database is locked')):
            self.assertEqual(result.record_result(self.db, payload)['status'], 'deferred')
            self.assertTrue(worker_supervision.run(self.config_path)['ok'])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'running')
        self.assertTrue(worker_supervision.run(self.config_path)['ok'])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'completed')
