"""Remote terminal facts and failures must not stall unrelated GitHub work."""
from contextlib import closing
import json
import sqlite3
import subprocess
import unittest
from unittest import mock

from tests import support  # noqa: F401
from tests import test_run_once as fixtures
from tests import test_retention as health_fixtures
from robert_agent import daemon_state, execution_store, reliability_health, run_once
from robert_agent.common import ControlDeadlineExceeded


class ReconcileIsolationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RunOnceDryRunTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.data_dir / 'dd.sqlite3'

    def seed(self, status='terminal'):
        result = run_once.run_once(self.fixture.config_path,
            fixture_path=self.fixture.fixture_path, dry_run=True, skip_external=True)
        self.assertTrue(result['ok'], result)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            task_id = conn.execute('SELECT task_id FROM tasks').fetchone()[0]
            conn.execute("UPDATE github_sources SET source_key='github:example/backend!123',source_type='pull_request',state='open'")
            conn.execute("UPDATE tasks SET lifecycle='completed'")
            conn.execute("UPDATE attempts SET status='completed',metadata_json='{}'")
            conn.execute("UPDATE workstreams SET lifecycle='completed',active_task_id=NULL")
            execution_store.close_binding(conn, 'task', task_id, '2026-10-28T07:00:00Z')
            conn.execute('UPDATE execution_subjects SET binding_status=?', (status,))
        return task_id

    @staticmethod
    def remote(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, json.dumps({
            'state': 'closed', 'merged': True, 'merged_at': '2026-10-28T07:10:33Z'}), '')

    def test_completed_or_attention_binding_records_merged_source_idempotently(self):
        task = self.seed()
        for binding_status in ('terminal', 'attention'):
            with self.subTest(binding_status=binding_status), closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE github_sources SET state='open'")
                conn.execute('UPDATE execution_subjects SET binding_status=?', (binding_status,))
                self.assertEqual(run_once._reconcile_remote_source_states(conn, 'repo:example/backend',
                    {'full_name': 'example/backend'}, self.remote, '2026-10-28T07:15:00Z'), 1)
                self.assertEqual(conn.execute('SELECT state FROM github_sources').fetchone()[0], 'closed')
                self.assertEqual(conn.execute('SELECT lifecycle FROM tasks WHERE task_id=?', (task,)).fetchone()[0], 'completed')
                self.assertEqual(conn.execute('SELECT binding_status FROM execution_subjects').fetchone()[0], binding_status)
                self.assertEqual(run_once._reconcile_remote_source_states(conn, 'repo:example/backend',
                    {'full_name': 'example/backend'}, self.remote, '2026-10-28T07:20:00Z'), 0)

    def test_bad_source_rolls_back_its_partial_writes_and_next_source_reconciles(self):
        self.seed()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO github_sources SELECT 'second',repo_id,'github:example/backend!124',source_type,124,html_url,title,state,author_login,source_updated_at,metadata_json FROM github_sources")
            conn.execute("INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,lifecycle,created_at,updated_at) VALUES ('second','repo:example/backend','second','completed','now','now')")
        record = run_once._record_remote_terminal_state
        def broken(conn, *args):
            if args[5] == 123:
                conn.execute("UPDATE github_sources SET title='partial' WHERE number=123")
                raise ValueError('broken source')
            return record(conn, *args)
        with closing(sqlite3.connect(self.db)) as conn, conn, mock.patch.object(run_once, '_record_remote_terminal_state', side_effect=broken):
            self.assertEqual(run_once._reconcile_remote_source_states(conn, 'repo:example/backend',
                {'full_name': 'example/backend'}, self.remote, '2026-10-28T07:15:00Z'), 1)
            rows = conn.execute('SELECT number,state,title FROM github_sources ORDER BY number').fetchall()
            self.assertEqual(rows[0][1], 'open')
            self.assertNotEqual(rows[0][2], 'partial')
            self.assertEqual(rows[1][1], 'closed')
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications WHERE notification_type='source_reconcile_failed'").fetchone()[0], 1)

    def test_repository_failure_does_not_commit_partial_writes_or_block_next_repo(self):
        import yaml
        config = yaml.safe_load(self.fixture.config_path.read_text())
        config['repos'].append({**config['repos'][0], 'full_name': 'example/second'})
        self.fixture.config_path.write_text(yaml.safe_dump(config))
        called = []
        def discover(repo, **kwargs):
            called.append(repo['full_name'])
            return []
        original = run_once._supervise_running_attempts
        def supervise(conn, repo_id, *args, **kwargs):
            if repo_id == 'repo:example/backend':
                conn.execute("UPDATE repos SET github_account='partial' WHERE repo_id=?", (repo_id,))
                raise ValueError('bad repository')
            return original(conn, repo_id, *args, **kwargs)
        with mock.patch.object(run_once, '_supervise_running_attempts', side_effect=supervise), \
                mock.patch.object(run_once.discover, 'collect_account_notifications', return_value={}), \
                mock.patch.object(run_once.discover, 'collect_live_events', side_effect=discover):
            result = run_once.run_once(self.fixture.config_path, dry_run=True,
                discovery_runner=lambda args, **kw: subprocess.CompletedProcess(args, 0, '{}', ''))
        self.assertFalse(result['ok'])
        self.assertIn('example/second', called)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT github_account FROM repos WHERE repo_id='repo:example/backend'").fetchone()[0], 'robert-bot')
            self.assertEqual(conn.execute("SELECT status FROM run_repo_steps WHERE repo_id='repo:example/backend' AND step_key='supervise'").fetchone()[0], 'failed')

    def test_attention_with_unproven_cleanup_records_source_but_keeps_ownership(self):
        task = self.seed('attention')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle='running'")
            conn.execute("UPDATE workstreams SET lifecycle='active',active_task_id=?", (task,))
            conn.execute("UPDATE attempts SET status='running',metadata_json='{}'")
        with closing(sqlite3.connect(self.db)) as conn, conn, mock.patch.object(
                run_once, '_terminate_owned_attempt', return_value={'status': 'attention'}):
            self.assertEqual(run_once._reconcile_remote_source_states(conn, 'repo:example/backend',
                {'full_name': 'example/backend'}, self.remote, '2026-10-28T07:15:00Z'), 0)
            self.assertEqual(conn.execute('SELECT state FROM github_sources').fetchone()[0], 'closed')
            self.assertEqual(conn.execute('SELECT active_task_id FROM workstreams').fetchone()[0], task)
            self.assertEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'running')
            self.assertEqual(conn.execute('SELECT binding_status FROM execution_subjects').fetchone()[0], 'attention')

    def test_source_failure_after_commit_rolls_back_suffix_and_keeps_next_source(self):
        self.seed()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO github_sources SELECT 'second',repo_id,'github:example/backend!124',source_type,124,html_url,title,state,author_login,source_updated_at,metadata_json FROM github_sources")
            conn.execute("INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,lifecycle,created_at,updated_at) VALUES ('second','repo:example/backend','second','completed','now','now')")
        original = run_once._record_remote_terminal_state
        def record(conn, *args):
            if args[5] == 123:
                conn.execute("UPDATE github_sources SET title='checkpoint' WHERE number=123")
                conn.commit()
                conn.execute("UPDATE github_sources SET title='uncommitted' WHERE number=123")
                raise RuntimeError('failure after external I/O checkpoint')
            return original(conn, *args)
        with closing(sqlite3.connect(self.db)) as conn, conn, mock.patch.object(run_once, '_record_remote_terminal_state', side_effect=record):
            self.assertEqual(run_once._reconcile_remote_source_states(conn, 'repo:example/backend',
                {'full_name': 'example/backend'}, self.remote, '2026-10-28T07:15:00Z'), 1)
            self.assertEqual(conn.execute('SELECT title FROM github_sources WHERE number=123').fetchone()[0], 'checkpoint')
            self.assertEqual(conn.execute('SELECT state FROM github_sources WHERE number=124').fetchone()[0], 'closed')

    def test_deadline_in_notification_collection_does_not_fall_back_to_repo_scan(self):
        with mock.patch.object(run_once.discover, 'collect_account_notifications',
                side_effect=ControlDeadlineExceeded('notifications', 0)), \
                mock.patch.object(run_once, '_run_repo_pipeline') as pipeline:
            with self.assertRaises(ControlDeadlineExceeded):
                run_once.run_once(self.fixture.config_path, dry_run=True)
            pipeline.assert_not_called()

    def test_control_deadline_is_not_isolated_as_an_ordinary_repository_failure(self):
        with mock.patch.object(run_once, '_supervise_running_attempts', side_effect=ControlDeadlineExceeded('probe', 0)):
            with self.assertRaises(ControlDeadlineExceeded):
                run_once.run_once(self.fixture.config_path, dry_run=True, skip_external=True)


class ControlFailureHealthTests(unittest.TestCase):
    def setUp(self):
        f = health_fixtures.RetentionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.db = f.db
        self.now = '2026-10-28T12:00:00+00:00'

    def event(self, event_type, status, minute):
        with closing(daemon_state.connect(self.db)) as conn, conn:
            daemon_state.record_event(conn, 'daemon', event_type, status,
                now=f'2026-10-28T11:{minute:02d}:00+00:00')

    def test_healthy_local_drains_do_not_hide_failed_scans(self):
        for minute in range(3):
            self.event('child_failed', 'failed', minute * 2)
            self.event('local_drain_completed', 'ok', minute * 2 + 1)
        self.assertEqual(reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])['control_failure_streak'], 3)

    def test_many_local_successes_do_not_resolve_scan_failure(self):
        from datetime import datetime, timedelta, timezone
        for minute in range(3):
            self.event('child_failed', 'failed', minute)
        reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])
        with closing(daemon_state.connect(self.db)) as conn, conn:
            for index in range(110):
                when = (datetime(2026, 10, 28, 11, 10, tzinfo=timezone.utc) + timedelta(seconds=index)).isoformat()
                daemon_state.record_event(conn, 'daemon', 'local_drain_completed', 'ok', now=when)
        self.assertEqual(reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])['control_failure_streak'], 3)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM notifications WHERE notification_type='control_plane_failed'").fetchone()[0], 'recorded')

    def test_interrupted_executors_alert_without_a_daemon_failure_event(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for minute in range(3):
                conn.execute("INSERT INTO agent_runs(run_id,status,started_at,finished_at,config_path,dry_run,error_json) VALUES (?,'failed',?,?,'/config',0,?)",
                    ('interrupted-' + str(minute), f'2026-10-28T11:{minute:02d}:00Z', f'2026-10-28T11:{minute:02d}:30Z',
                     json.dumps({'status': 'executor_interrupted'})))
        self.assertEqual(reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])['control_failure_streak'], 3)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications WHERE notification_type='control_plane_failed'").fetchone()[0], 1)

    def test_repeated_failures_alert_once_until_success_and_can_alert_again(self):
        for minute in range(3):
            self.event('child_failed', 'failed', minute)
        for _ in range(2):
            health = reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])
            self.assertEqual(health['control_failure_streak'], 3)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications WHERE notification_type='control_plane_failed'").fetchone()[0], 1)
        self.event('live_poll_completed', 'ok', 3)
        self.assertEqual(reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])['control_failure_streak'], 0)
        self.event('child_failed', 'failed', 4)
        self.event('child_failed', 'failed', 5)
        self.assertEqual(reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])['control_failure_streak'], 2)
        self.event('child_failed', 'failed', 6)
        reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications WHERE notification_type='control_plane_failed'").fetchone()[0], 2)
