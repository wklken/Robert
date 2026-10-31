"""Kanban operator requests reach GitHub attempts without polling empty work."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from tests import test_board_control_integration
from robert_agent import board, execution_store, github_scan, loop_engine, run_once, work_items


class GitHubBoardRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_board_control_integration.BoardControlIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.db_path
        event = {'number': 70, 'source_type': 'issue', 'event_type': 'assigned',
                 'id': '70', 'assigned_to': 'robot', 'assignment_actor_login': 'owner',
                 'actor_login': 'owner', 'title': 'Draw diagrams', 'body': '',
                 'url': 'https://github.com/example/repo/issues/70'}
        source_fixture = self.fixture.root / 'source.json'
        source_fixture.write_text(json.dumps({'events': [event]}))
        result = run_once.run_once(self.fixture.config_path, fixture_path=source_fixture,
                                  dry_run=True, skip_external=True)
        self.assertTrue(result['ok'], result)
        with closing(sqlite3.connect(self.db)) as c, c:
            self.parent = c.execute('SELECT task_id FROM tasks').fetchone()[0]
            self.original = {p: Path(p).read_bytes() for p, in c.execute('SELECT path FROM artifacts')}
            c.execute("UPDATE tasks SET lifecycle='waiting_for_user' WHERE task_id=?", (self.parent,))
            c.execute("UPDATE attempts SET status='completed',finished_at=? WHERE task_id=?", (run_once._now(), self.parent))
            c.execute("UPDATE workstreams SET lifecycle='waiting_for_user'")
            self.item = work_items.resolve_work_item_for_workstream(c, 'github:example/repo#70')
            for (decision,) in c.execute("SELECT event_id FROM work_item_events WHERE event_type='operator_decision_required'").fetchall():
                work_items.record_system_event(c, self.item['work_item_id'], event_type='operator_approved',
                    idempotency_key='accept-' + decision, resolves_event_id=decision, now=run_once._now())
            work_items.record_system_event(c, self.item['work_item_id'], event_type='operator_question',
                idempotency_key='question', body='Which architecture?', now=run_once._now())
        self.reply = work_items.execute_command(self.db, self.item['work_item_id'],
            context=self.fixture.context, command='reply', expected_version=self.item['version'],
            idempotency_key='reply', body='Read the full issue body and draw the architecture.')
        self.task_id = self.reply['task_id']

    def read(self, payload=None, *, fresh=True):
        payload = payload if payload is not None else {'body': 'Analyze the architecture and draw sequence diagrams.',
            'title': 'Draw diagrams', 'state': 'open', 'updated_at': '2026-10-31T01:00:00Z'}
        with closing(sqlite3.connect(self.db)) as c, c:
            requested = c.execute('SELECT created_at FROM wakeups WHERE task_id=?', (self.task_id,)).fetchone()[0]
            when = datetime.fromisoformat(requested).timestamp() + (1 if fresh else -1)
            github_scan._save_reads(c, self.fixture.repo_id,
                {github_scan.command_key(['gh', 'api', 'repos/example/repo/issues/70']): json.dumps(payload)}, when)

    def cycle(self):
        return self.fixture._run_local_cycle()

    def test_reply_prepares_existing_task_with_body_and_operator_response(self):
        self.read()
        result = self.cycle()
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM attempts WHERE task_id=?', (self.task_id,)).fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT count(*) FROM tasks').fetchone()[0], 2)
            path = c.execute("SELECT path FROM artifacts WHERE task_id=? AND artifact_type='prompt'", (self.task_id,)).fetchone()[0]
            self.assertEqual(c.execute('SELECT status FROM wakeups WHERE task_id=?', (self.task_id,)).fetchone()[0], 'consumed')
            self.assertIsNotNone(execution_store.get_binding(c, 'task', self.task_id))
        text = Path(path).read_text()
        self.assertIn('Analyze the architecture and draw sequence diagrams.', text)
        self.assertIn('Read the full issue body and draw the architecture.', text)
        self.assertIn('origin_type: github', text)
        self.assertEqual(self.original, {p: Path(p).read_bytes() for p in self.original})
        self.assertTrue(result['prompt_paths'])
        self.cycle()
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM attempts WHERE task_id=?', (self.task_id,)).fetchone()[0], 1)

    def test_waiting_for_fresh_scan_does_not_wake_local_drain(self):
        self.read(fresh=False)
        with closing(sqlite3.connect(self.db)) as c:
            self.assertFalse(loop_engine.has_runnable_local_work(c, repo_id=self.fixture.repo_id,
                                                               include_publication=False))
        result = self.cycle()
        self.assertFalse(result['prompt_paths'])
        self.read()
        with closing(sqlite3.connect(self.db)) as c:
            self.assertTrue(loop_engine.has_runnable_local_work(c, repo_id=self.fixture.repo_id,
                                                              include_publication=False))

    def test_failed_refresh_is_visible_and_stops_empty_drains(self):
        self.read({'state': 'open'})
        self.cycle()
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT lifecycle FROM tasks WHERE task_id=?', (self.task_id,)).fetchone()[0], 'failed')
            self.assertEqual(c.execute("SELECT count(*) FROM work_item_events WHERE event_type='execution_failed'").fetchone()[0], 1)
            self.assertFalse(loop_engine.has_runnable_local_work(c, repo_id=self.fixture.repo_id,
                                                               include_publication=False))

    def test_cancel_before_scan_does_not_launch_an_attempt(self):
        work_items.execute_command(self.db, self.item['work_item_id'], context=self.fixture.context,
            command='cancel', expected_version=self.reply['item']['version'], idempotency_key='cancel')
        self.read()
        self.cycle()
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM attempts WHERE task_id=?', (self.task_id,)).fetchone()[0], 0)

    def test_board_explains_waiting_for_github_refresh(self):
        detail = board.get_work_item_detail(self.db, self.item['work_item_id'])
        self.assertEqual(detail['reason_code'], 'github_context_refresh_pending')
        self.read()
        detail = board.get_work_item_detail(self.db, self.item['work_item_id'])
        self.assertEqual(detail['reason_code'], 'detected')

    def test_failed_refresh_can_be_retried_from_board(self):
        self.read({'state': 'open'})
        self.cycle()
        item = board.get_work_item_detail(self.db, self.item['work_item_id'])
        self.assertIn('retry', item['valid_commands'])
        retried = work_items.execute_command(self.db, self.item['work_item_id'],
            context=self.fixture.context, command='retry', expected_version=item['version'],
            idempotency_key='retry')
        failed = self.task_id
        self.task_id = retried['task_id']
        self.read()
        self.cycle()
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT lifecycle FROM tasks WHERE task_id=?', (failed,)).fetchone()[0], 'completed')
            self.assertEqual(c.execute('SELECT count(*) FROM attempts WHERE task_id=?', (self.task_id,)).fetchone()[0], 1)

    def test_parent_worker_or_attention_blocks_recovery_without_empty_drains(self):
        self.read()
        for kind in ('worker', 'attention'):
            with self.subTest(kind=kind):
                with closing(sqlite3.connect(self.db)) as c, c:
                    c.execute("UPDATE attempts SET status=? WHERE task_id=?", ('running' if kind == 'worker' else 'completed', self.parent))
                    c.execute('UPDATE tasks SET metadata_json=? WHERE task_id=?',
                        (json.dumps({'execution_attention': {'reason_code': 'worker_identity_attention'}}) if kind == 'attention' else '{}', self.parent))
                self.cycle()
                with closing(sqlite3.connect(self.db)) as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM attempts WHERE task_id=?', (self.task_id,)).fetchone()[0], 0)
                    if kind == 'attention':
                        self.assertFalse(loop_engine.has_runnable_local_work(c, repo_id=self.fixture.repo_id, include_publication=False))

    def test_cancellation_during_worktree_io_keeps_task_and_never_starts(self):
        self.read()
        def cancel(*args, **kwargs):
            with closing(sqlite3.connect(self.db)) as c, c:
                c.execute("UPDATE tasks SET lifecycle='canceled' WHERE task_id=?", (self.task_id,))
                c.execute("UPDATE work_items SET canceled_at=?", (run_once._now(),))
                c.execute("UPDATE wakeups SET status='canceled' WHERE task_id=?", (self.task_id,))
            return None
        with patch.object(run_once, '_prepare_worktree', side_effect=cancel):
            self.cycle()
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT lifecycle FROM tasks WHERE task_id=?', (self.task_id,)).fetchone()[0], 'canceled')
            self.assertEqual(c.execute('SELECT count(*) FROM attempts WHERE task_id=?', (self.task_id,)).fetchone()[0], 0)

    def test_changed_trust_stops_operator_request(self):
        self.read()
        config = json.loads(self.fixture.config_path.read_text())
        config['repos'][0]['trusted_actors'] = ['another-owner']
        self.fixture.config_path.write_text(json.dumps(config))
        self.cycle()
        with closing(sqlite3.connect(self.db)) as c:
            row = c.execute("SELECT metadata_json FROM work_item_events WHERE event_type='execution_failed'").fetchone()
            self.assertEqual(json.loads(row[0])['reason_code'], 'github_operator_authorization_unavailable')

    def test_notification_does_not_reuse_old_trust_boolean(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            for event_id, raw in c.execute('SELECT event_id,payload_json FROM github_events').fetchall():
                event = json.loads(raw)
                event.update(event_type='notification', trusted_trigger_found=True)
                c.execute('UPDATE github_events SET payload_json=? WHERE event_id=?', (json.dumps(event),event_id))
        self.test_changed_trust_stops_operator_request()

    def test_reopen_and_request_changes_prepare_github_tasks(self):
        for command in ('reopen', 'request_changes'):
            with self.subTest(command=command):
                self.fixture._finish_task(self.task_id, run_once._now())
                with closing(sqlite3.connect(self.db)) as c, c:
                    c.execute("UPDATE workstreams SET lifecycle='completed',active_task_id=NULL")
                    if command == 'reopen':
                        c.execute('UPDATE work_items SET completed_at=?', (run_once._now(),))
                    else:
                        work_items.record_system_event(c, self.item['work_item_id'], event_type='operator_question',
                            idempotency_key='changes-question', now=run_once._now())
                item = board.get_work_item_detail(self.db, self.item['work_item_id'])
                response = work_items.execute_command(self.db, self.item['work_item_id'],
                    context=self.fixture.context, command=command, expected_version=item['version'],
                    idempotency_key=command, body='Please update the diagrams.')
                self.task_id = response['task_id']
                self.read()
                self.cycle()
                with closing(sqlite3.connect(self.db)) as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM attempts WHERE task_id=?', (self.task_id,)).fetchone()[0], 1)

    def test_duplicate_reply_reuses_queued_task(self):
        duplicate = work_items.execute_command(self.db, self.item['work_item_id'],
            context=self.fixture.context, command='reply', expected_version=self.item['version'],
            idempotency_key='reply', body='Read the full issue body and draw the architecture.')
        self.assertEqual(duplicate['task_id'], self.task_id)
        self.read()
        self.cycle()
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM tasks').fetchone()[0], 2)

    def test_manual_worker_priority_and_parent_are_preserved(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE tasks SET routing_mode='manual',requested_worker='reviewer',priority='P2' WHERE task_id=?", (self.task_id,))
        self.read()
        self.cycle()
        with closing(sqlite3.connect(self.db)) as c:
            row = c.execute('SELECT routing_mode,requested_worker,priority,parent_task_id FROM tasks WHERE task_id=?', (self.task_id,)).fetchone()
            self.assertEqual(row, ('manual','reviewer','P2',self.parent))

    def test_closed_source_and_failed_read_stop_once(self):
        for payload, reason in (({'state': 'closed', 'body': 'body'}, 'github_operator_source_closed'),
                                ('null', 'github_operator_context_unavailable')):
            with self.subTest(payload=payload):
                if payload == 'null':
                    with closing(sqlite3.connect(self.db)) as c, c:
                        c.execute("UPDATE tasks SET lifecycle='detected' WHERE task_id=?", (self.task_id,))
                        c.execute("UPDATE workstreams SET active_task_id=?,lifecycle='active'", (self.task_id,))
                        c.execute("UPDATE wakeups SET status='pending' WHERE task_id=?", (self.task_id,))
                        c.execute("DELETE FROM execution_subjects WHERE subject_id=?", (self.task_id,))
                        c.execute("DELETE FROM work_item_events WHERE event_type='execution_failed'")
                    self.read({'state': 'open'})
                    with closing(sqlite3.connect(self.db)) as c, c:
                        c.execute('UPDATE github_scan_reads SET response_json=NULL')
                else:
                    self.read(payload)
                self.cycle()
                with closing(sqlite3.connect(self.db)) as c:
                    row = c.execute("SELECT metadata_json FROM work_item_events WHERE event_type='execution_failed'").fetchone()
                    self.assertEqual(json.loads(row[0])['reason_code'], reason)


if __name__ == '__main__':
    unittest.main()
