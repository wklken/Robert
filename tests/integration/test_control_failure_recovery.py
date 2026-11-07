from contextlib import closing
import json
import sqlite3
import subprocess
import unittest
from unittest import mock
from tests import support  # noqa: F401
from tests.support import reconcile_isolation as fixtures
from robert_agent.runtime import cycle as run_once
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.execution.application import monitor
from robert_agent.work.application import source_lifecycle


class ReviewRemediationTests(unittest.TestCase):
    setUp = fixtures.ReconcileIsolationFixture.setUp
    seed = fixtures.ReconcileIsolationFixture.seed
    remote = staticmethod(fixtures.ReconcileIsolationFixture.remote)

    def test_deadline_after_checkpoint_releases_lease_and_finishes_run(self):
        def supervise(conn, *args, **kwargs):
            conn.execute("UPDATE repos SET github_account='checkpoint'")
            conn.commit()
            conn.execute("UPDATE repos SET github_account='uncommitted'")
            raise ControlDeadlineExceeded('probe', 0)
        with mock.patch.object(monitor, 'supervise_running_attempts', side_effect=supervise):
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
        with mock.patch.object(source_lifecycle, 'record_remote_terminal_state', side_effect=ValueError('bad source')):
            for _ in range(3):
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    source_lifecycle.reconcile_remote_source_states(conn, 'repo:example/backend',
                        {'full_name': 'example/backend'}, self.remote, '2026-10-28T07:15:00Z')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications WHERE notification_type='source_reconcile_failed'").fetchone()[0], 1)
            source_lifecycle.reconcile_remote_source_states(conn, 'repo:example/backend',
                {'full_name': 'example/backend'}, self.remote, '2026-10-28T07:20:00Z')
            self.assertEqual(conn.execute("SELECT status FROM notifications WHERE notification_type='source_reconcile_failed'").fetchone()[0], 'resolved')

    def test_repeated_repo_failure_has_one_notice_and_success_resolves_it(self):
        with mock.patch.object(monitor, 'supervise_running_attempts', side_effect=ValueError('bad repo')):
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
                monitor, 'terminate_owned_attempt', return_value={'status': 'terminated'}) as terminate:
            for _ in range(3):
                self.assertEqual(source_lifecycle.reconcile_remote_source_states(conn, 'repo:example/backend',
                    {'full_name': 'example/backend'}, remote, '2026-10-28T07:15:00Z'), 0)
            self.assertEqual(conn.execute('SELECT state FROM github_sources').fetchone()[0], 'closed')
            self.assertEqual(conn.execute('SELECT lifecycle FROM tasks').fetchone()[0], 'running')
            self.assertEqual(conn.execute('SELECT binding_status FROM execution_subjects').fetchone()[0], 'attention')
            self.assertEqual(conn.execute("SELECT count(*) FROM audit_events WHERE event_type='remote_source_closed'").fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT completed_at,canceled_at FROM work_items').fetchone(), (None, None))
            terminate.assert_not_called()
