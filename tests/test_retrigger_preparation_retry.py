"""A failed explicit recovery request must back off or stop visibly."""
from contextlib import closing
import json
import sqlite3
import subprocess
import unittest
from unittest import mock

from tests import test_terminal_github_followup as fixtures
from robert_agent import board, loop_engine, run_once, validate_config, work_items


class RetriggerPreparationRetryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TerminalGithubFollowupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.finish('failed')
        self.db = self.fixture.db_path
        self.request = work_items.retrigger_failed_github_task(self.db, self.fixture.parent_id,
            context=self.fixture.fixture.context, idempotency_key='recover', notifier=lambda _: None)
        self.config = validate_config.validate_config(self.fixture.fixture.config_path, skip_external=True)

    def cycle(self):
        return run_once.run_once(self.fixture.fixture.config_path, fixture_path=self.fixture.fixture.empty_fixture,
            dry_run=True, skip_external=True, skip_publish=True)

    def state(self):
        with closing(sqlite3.connect(self.db)) as conn:
            row = conn.execute('SELECT status,not_before_at,metadata_json FROM wakeups WHERE wakeup_id=?',
                (self.request['wakeup_id'],)).fetchone()
        return row[0], row[1], json.loads(row[2])

    def test_permanent_error_stops_once_and_exposes_retrigger(self):
        with mock.patch.object(run_once, '_prepare_worktree', side_effect=subprocess.CalledProcessError(1, ['git', 'fetch'], stderr='permission denied')) as prepare:
            for _ in range(5):
                self.cycle()
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(self.state()[0], 'canceled')
        with closing(sqlite3.connect(self.db)) as conn:
            item = conn.execute('SELECT work_item_id FROM work_items').fetchone()[0]
            self.assertFalse(loop_engine.has_runnable_local_work(conn, repo_ids=[self.fixture.fixture.repo_id], config_result=self.config))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications WHERE notification_type='github_retrigger_failed'").fetchone()[0], 1)
        self.assertIn('retrigger', board.get_work_item_detail(self.db, item)['valid_commands'])

    def test_transient_error_backs_off_and_budget_survives_cycles(self):
        for count in range(1, 4):
            status, due, _ = self.state()
            with mock.patch.object(run_once, '_now', return_value=due), mock.patch.object(run_once, '_prepare_worktree',
                    side_effect=subprocess.TimeoutExpired(['git', 'fetch'], 1)):
                self.cycle()
            status, _, metadata = self.state()
            retry = metadata.get('preparation_retry', {})
            self.assertEqual(retry.get('attempt_count'), count, retry)
            self.assertEqual(status, 'pending' if count < 3 else 'canceled')
            if count < 3:
                with mock.patch.object(run_once, '_now', return_value=retry['failed_at']), mock.patch.object(run_once, '_prepare_worktree') as prepare:
                    self.cycle()
                    prepare.assert_not_called()

    def test_retry_after_transient_error_creates_only_one_child(self):
        with mock.patch.object(run_once, '_prepare_worktree', side_effect=subprocess.TimeoutExpired(['git', 'fetch'], 1)):
            self.cycle()
        with mock.patch.object(run_once, '_now', return_value=self.state()[1]):
            self.cycle()
            self.cycle()
        self.assertEqual(self.state()[0], 'consumed')
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks WHERE parent_task_id=?', (self.fixture.parent_id,)).fetchone()[0], 1)
