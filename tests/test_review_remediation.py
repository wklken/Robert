"""Regression evidence for the follow-up review of PR #66."""
from contextlib import closing
import json
import sqlite3
import subprocess
import unittest
from unittest import mock

from tests import support  # noqa: F401
from tests import test_reconcile_isolation as fixtures
from robert_agent import daemon_state, reliability_health, run_once
from robert_agent.common import ControlDeadlineExceeded


class ReviewRemediationTests(unittest.TestCase):
    setUp = fixtures.ReconcileIsolationTests.setUp
    seed = fixtures.ReconcileIsolationTests.seed
    remote = staticmethod(fixtures.ReconcileIsolationTests.remote)

    def test_deadline_after_checkpoint_releases_lease_and_finishes_run(self):
        def supervise(conn, *args, **kwargs):
            conn.execute("UPDATE repos SET github_account='checkpoint'")
            conn.commit()
            conn.execute("UPDATE repos SET github_account='uncommitted'")
            raise ControlDeadlineExceeded('probe', 0)
        with mock.patch.object(run_once, '_supervise_running_attempts', side_effect=supervise):
            with self.assertRaises(ControlDeadlineExceeded):
                run_once.run_once(self.fixture.config_path, dry_run=True, skip_external=True)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM leases WHERE resource_type='agent_run'").fetchone()[0], 'released')
            status, error = conn.execute('SELECT status,error_json FROM agent_runs').fetchone()
            self.assertEqual(status, 'failed')
            self.assertEqual(json.loads(error)['status'], 'execution_deadline')
            self.assertEqual(conn.execute('SELECT github_account FROM repos').fetchone()[0], 'checkpoint')
            self.assertEqual(conn.execute("SELECT count(*) FROM run_steps WHERE status IN ('running','pending')").fetchone()[0], 0)

    def test_repeated_source_failure_has_one_notice_and_success_resolves_it(self):
        self.seed()
        with mock.patch.object(run_once, '_record_remote_terminal_state', side_effect=ValueError('bad source')):
            for _ in range(3):
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    run_once._reconcile_remote_source_states(conn, 'repo:example/backend',
                        {'full_name': 'example/backend'}, self.remote, '2026-10-28T07:15:00Z')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications WHERE notification_type='source_reconcile_failed'").fetchone()[0], 1)
            run_once._reconcile_remote_source_states(conn, 'repo:example/backend',
                {'full_name': 'example/backend'}, self.remote, '2026-10-28T07:20:00Z')
            self.assertEqual(conn.execute("SELECT status FROM notifications WHERE notification_type='source_reconcile_failed'").fetchone()[0], 'resolved')

    def test_repeated_repo_failure_has_one_notice_and_success_resolves_it(self):
        with mock.patch.object(run_once, '_supervise_running_attempts', side_effect=ValueError('bad repo')):
            for _ in range(3):
                run_once.run_once(self.fixture.config_path, dry_run=True, skip_external=True)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications WHERE notification_type='repository_pipeline_failed'").fetchone()[0], 1)
        self.assertTrue(run_once.run_once(self.fixture.config_path, dry_run=True, skip_external=True)['ok'])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM notifications WHERE notification_type='repository_pipeline_failed'").fetchone()[0], 'resolved')

    def test_attention_closed_issue_records_only_source_once(self):
        task = self.seed('attention')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE github_sources SET source_type='issue',source_key='github:example/backend#123'")
            conn.execute("UPDATE tasks SET lifecycle='running'")
            conn.execute("UPDATE attempts SET status='running',metadata_json='{}'")
            conn.execute("UPDATE workstreams SET lifecycle='active',active_task_id=?", (task,))
        def remote(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, json.dumps({
                'state': 'closed', 'state_reason': 'completed', 'closed_at': '2026-10-28T07:10:33Z'}), '')
        with closing(sqlite3.connect(self.db)) as conn, conn, mock.patch.object(
                run_once, '_terminate_owned_attempt', return_value={'status': 'terminated'}) as terminate:
            for _ in range(3):
                self.assertEqual(run_once._reconcile_remote_source_states(conn, 'repo:example/backend',
                    {'full_name': 'example/backend'}, remote, '2026-10-28T07:15:00Z'), 0)
            self.assertEqual(conn.execute('SELECT state FROM github_sources').fetchone()[0], 'closed')
            self.assertEqual(conn.execute('SELECT lifecycle FROM tasks').fetchone()[0], 'running')
            self.assertEqual(conn.execute('SELECT binding_status FROM execution_subjects').fetchone()[0], 'attention')
            self.assertEqual(conn.execute("SELECT count(*) FROM audit_events WHERE event_type='remote_source_closed'").fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT completed_at,canceled_at FROM work_items').fetchone(), (None, None))
            terminate.assert_not_called()


class ReviewHealthTests(unittest.TestCase):
    setUp = fixtures.ControlFailureHealthTests.setUp
    event = fixtures.ControlFailureHealthTests.event

    def test_non_crash_failures_are_counted_per_lane(self):
        for lane in ('scan', 'local'):
            for index, kind in enumerate(('child_timeout', 'partial_failure', 'repository_pipeline_failed')):
                with closing(daemon_state.connect(self.db)) as conn, conn:
                    daemon_state.record_event(conn, 'daemon', kind, 'failed', {'control_lane': lane},
                        now=f'2026-10-28T11:{index:02d}:00+00:00')
        self.assertEqual(reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])['control_failure_streak'], 3)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications WHERE notification_type='control_plane_failed'").fetchone()[0], 2)

    def test_writer_contention_does_not_drop_health_measurements(self):
        for minute in range(3):
            self.event('child_failed', 'failed', minute)
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            health = reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])
            self.assertGreater(health['database_bytes'], 0)
            self.assertEqual(health['control_failure_streak'], 3)
            self.assertFalse(health['alerts_persisted'])
        self.assertTrue(reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])['alerts_persisted'])

    def test_alert_write_contention_retries_at_local_poll_interval(self):
        from tests import test_retention as health_fixtures
        from robert_agent import daemon
        fixture = health_fixtures.RetentionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with mock.patch.object(daemon.retention, 'prune_database', return_value={
                'ok': True, 'status': 'pruned', 'counts': {}}), \
             mock.patch.object(reliability_health, 'collect', return_value={'alerts_persisted': False}), \
             mock.patch.object(daemon.time, 'monotonic', return_value=100):
            daemon._maybe_prune(fixture.context, now=self.now)
        expected = 100 + fixture.context.daemon_config['local_poll_seconds']
        self.assertEqual(fixture.context.next_health_at, expected)
        self.assertEqual(fixture.context.next_maintenance_at, expected)
