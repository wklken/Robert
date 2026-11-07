"""Remote terminal facts and failures must not stall unrelated GitHub work."""
from contextlib import closing
import sqlite3
import subprocess
from unittest import mock

from tests import support  # noqa: F401
from robert_agent.runtime import cycle as run_once
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.adapters.github import discovery
from robert_agent.execution.application import monitor
from robert_agent.work.application import source_lifecycle
from robert_agent.application import repository_cycle
from tests.support.reconcile_isolation import ReconcileIsolationFixture


class ReconcileIsolationTests(ReconcileIsolationFixture):
    def test_completed_or_attention_binding_records_merged_source_idempotently(self):
        task = self.seed()
        for binding_status in ('terminal', 'attention'):
            with self.subTest(binding_status=binding_status), closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE github_sources SET state='open'")
                conn.execute('UPDATE execution_subjects SET binding_status=?', (binding_status,))
                self.assertEqual(source_lifecycle.reconcile_remote_source_states(conn, 'repo:example/backend',
                    {'full_name': 'example/backend'}, self.remote, '2026-10-28T07:15:00Z'),
                    1 if binding_status == 'terminal' else 0)
                self.assertEqual(conn.execute('SELECT state FROM github_sources').fetchone()[0], 'closed')
                self.assertEqual(conn.execute('SELECT lifecycle FROM tasks WHERE task_id=?', (task,)).fetchone()[0], 'completed')
                self.assertEqual(conn.execute('SELECT binding_status FROM execution_subjects').fetchone()[0], binding_status)
                self.assertEqual(source_lifecycle.reconcile_remote_source_states(conn, 'repo:example/backend',
                    {'full_name': 'example/backend'}, self.remote, '2026-10-28T07:20:00Z'), 0)

    def test_bad_source_rolls_back_its_partial_writes_and_next_source_reconciles(self):
        self.seed()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO github_sources SELECT 'second',repo_id,'github:example/backend!124',source_type,124,html_url,title,state,author_login,source_updated_at,metadata_json FROM github_sources")
            conn.execute("INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,lifecycle,created_at,updated_at) VALUES ('second','repo:example/backend','second','completed','now','now')")
        record = source_lifecycle.record_remote_terminal_state
        def broken(conn, *args):
            if args[5] == 123:
                conn.execute("UPDATE github_sources SET title='partial' WHERE number=123")
                raise ValueError('broken source')
            return record(conn, *args)
        with closing(sqlite3.connect(self.db)) as conn, conn, mock.patch.object(source_lifecycle, 'record_remote_terminal_state', side_effect=broken):
            self.assertEqual(source_lifecycle.reconcile_remote_source_states(conn, 'repo:example/backend',
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
        original = monitor.supervise_running_attempts
        def supervise(conn, repo_id, *args, **kwargs):
            if repo_id == 'repo:example/backend':
                conn.execute("UPDATE repos SET github_account='partial' WHERE repo_id=?", (repo_id,))
                raise ValueError('bad repository')
            return original(conn, repo_id, *args, **kwargs)
        with mock.patch.object(monitor, 'supervise_running_attempts', side_effect=supervise), \
                mock.patch.object(discovery, 'collect_account_notifications', return_value={}), \
                mock.patch.object(discovery, 'collect_live_events', side_effect=discover):
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
                monitor, 'terminate_owned_attempt', return_value={'status': 'attention'}):
            self.assertEqual(source_lifecycle.reconcile_remote_source_states(conn, 'repo:example/backend',
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
        original = source_lifecycle.record_remote_terminal_state
        def record(conn, *args):
            if args[5] == 123:
                conn.execute("UPDATE github_sources SET title='checkpoint' WHERE number=123")
                conn.commit()
                conn.execute("UPDATE github_sources SET title='uncommitted' WHERE number=123")
                raise RuntimeError('failure after external I/O checkpoint')
            return original(conn, *args)
        with closing(sqlite3.connect(self.db)) as conn, conn, mock.patch.object(source_lifecycle, 'record_remote_terminal_state', side_effect=record):
            self.assertEqual(source_lifecycle.reconcile_remote_source_states(conn, 'repo:example/backend',
                {'full_name': 'example/backend'}, self.remote, '2026-10-28T07:15:00Z'), 1)
            self.assertEqual(conn.execute('SELECT title FROM github_sources WHERE number=123').fetchone()[0], 'checkpoint')
            self.assertEqual(conn.execute('SELECT state FROM github_sources WHERE number=124').fetchone()[0], 'closed')

    def test_deadline_in_notification_collection_does_not_fall_back_to_repo_scan(self):
        with mock.patch.object(discovery, 'collect_account_notifications',
                side_effect=ControlDeadlineExceeded('notifications', 0)), \
                mock.patch.object(repository_cycle, 'run_repo_pipeline') as pipeline:
            with self.assertRaises(ControlDeadlineExceeded):
                run_once.run_once(self.fixture.config_path, dry_run=True)
            pipeline.assert_not_called()

    def test_control_deadline_is_not_isolated_as_an_ordinary_repository_failure(self):
        with mock.patch.object(monitor, 'supervise_running_attempts', side_effect=ControlDeadlineExceeded('probe', 0)):
            with self.assertRaises(ControlDeadlineExceeded):
                run_once.run_once(self.fixture.config_path, dry_run=True, skip_external=True)
