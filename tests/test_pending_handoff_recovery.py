"""Accepted results retain ordinary pending handoffs across preparation failures."""
from contextlib import closing
import json
import sqlite3
import subprocess
import unittest
from unittest import mock

from tests import test_run_once as fixtures
from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import board, loop_engine, run_once, validate_config, work_items
from robert_agent.worker import result as worker_result


class PendingHandoffRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RunOnceDryRunTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.data_dir / 'dd.sqlite3'
        self.cycle(self.fixture.fixture_path)
        with closing(sqlite3.connect(self.db)) as conn:
            self.task, self.attempt = conn.execute('SELECT task_id,attempt_id FROM attempts').fetchone()
        self.fixture.fixture_path.write_text(json.dumps({'events': [{
            'id': 'pending', 'number': 123, 'source_type': 'issue', 'event_type': 'comment',
            'actor_login': 'wklken', 'body': '@robert-bot implement the second change',
            'intent': 'bug_fix', 'state': 'open',
        }]}))
        self.cycle(self.fixture.fixture_path)
        receipt = worker_result.record_result(self.db, {
            'task_id': self.task, 'attempt_id': self.attempt, 'output_type': 'new_pr',
            'submission': {
                'planned_github_actions': self.fixture._new_pr_actions_for_attempt(self.db, self.task),
                'consumed_event_fingerprints': ['comment:comment-1'],
                'verification': [fixtures._verification_evidence()], 'handoff': 'done',
                'used_skills': ['fast-small-pr'],
            },
        })
        self.assertTrue(receipt['ok'], receipt)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE github_actions SET publish_status='published'")
        self.empty = self.fixture.root / 'empty.json'
        self.empty.write_text('{"events":[]}')
        self.config = validate_config.validate_config(self.fixture.config_path, skip_external=True)

    def cycle(self, fixture=None):
        return run_once.run_once(self.fixture.config_path, fixture_path=fixture,
            dry_run=True, skip_external=True, skip_publish=True, consume_observations=True)

    def state(self):
        with closing(sqlite3.connect(self.db)) as conn:
            task = conn.execute('SELECT lifecycle,metadata_json FROM tasks WHERE task_id=?', (self.task,)).fetchone()
            return task[0], json.loads(task[1]).get('pending_handoff', {})

    def test_transient_handoff_backs_off_then_recovers_once(self):
        with mock.patch.object(run_once, '_prepare_worktree', side_effect=subprocess.TimeoutExpired(['git', 'fetch'], 1)):
            self.cycle(self.empty)
        lifecycle, handoff = self.state()
        self.assertEqual(lifecycle, 'completed')
        self.assertTrue(handoff.get('retryable'), handoff)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertFalse(loop_engine.has_runnable_local_work(conn, repo_ids=['repo:example/backend'], config_result=self.config, now=handoff['failed_at']))
            self.assertTrue(loop_engine.has_runnable_local_work(conn, repo_ids=['repo:example/backend'], config_result=self.config, now=handoff['next_retry_at']))
        with mock.patch.object(run_once, '_now', return_value=handoff['next_retry_at']):
            self.assertTrue(self.cycle(self.empty)['ok'])
            self.assertTrue(self.cycle(self.empty)['ok'])
        with closing(sqlite3.connect(self.db)) as conn:
            children = conn.execute('SELECT task_id FROM tasks WHERE parent_task_id=?', (self.task,)).fetchall()
            self.assertEqual(len(children), 1)
            self.assertEqual(conn.execute('SELECT relationship FROM task_events WHERE task_id=? AND event_id=?',
                (children[0][0], 'event:comment:pending')).fetchone()[0], 'trigger')
        self.assertEqual(self.state()[1]['status'], 'completed')

    def test_permanent_failure_is_visible_and_operator_can_retry(self):
        with mock.patch.object(run_once, '_prepare_worktree', side_effect=subprocess.CalledProcessError(1, ['git', 'fetch'], stderr='permission denied')):
            self.cycle(self.empty)
        lifecycle, handoff = self.state()
        self.assertEqual(lifecycle, 'completed')
        self.assertFalse(handoff.get('retryable', True), handoff)
        with closing(sqlite3.connect(self.db)) as conn:
            item_id = conn.execute('SELECT work_item_id FROM work_items').fetchone()[0]
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM work_item_events WHERE event_type='execution_failed'").fetchone()[0], 1)
        card = board.get_work_item_detail(self.db, item_id)
        self.assertIn('retry', card['valid_commands'], card)
        context = work_items.CommandContext('operator', 'owner', frozenset({'repo:example/backend'}), frozenset({'default'}))
        retry = work_items.execute_command(self.db, item_id, context=context, command='retry',
            expected_version=card['version'], idempotency_key='retry-handoff', notifier=lambda _: None)
        self.assertTrue(retry['ok'], retry)

    def test_historical_orphan_without_retry_metadata_is_recovered(self):
        # This is the persisted state left by the old finalizer after its Git commit boundary.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle='completed'")
            conn.execute("UPDATE worker_results SET metadata_json=json_set(metadata_json,'$.audit.status','accepted')")
            conn.execute("UPDATE github_actions SET audit_status='accepted'")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertTrue(loop_engine.has_runnable_local_work(conn, repo_ids=['repo:example/backend'], config_result=self.config))
        self.cycle(self.empty)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks WHERE parent_task_id=?', (self.task,)).fetchone()[0], 1)

    def test_transient_budget_stops_and_explicit_retry_processes_pending_request(self):
        due = run_once._now()
        for count in range(1, 4):
            with mock.patch.object(run_once, '_now', return_value=due), mock.patch.object(run_once, '_prepare_worktree',
                    side_effect=subprocess.TimeoutExpired(['git', 'fetch'], 1)):
                self.cycle(self.empty)
            state = self.state()[1]
            self.assertEqual(state['attempt_count'], count)
            due = state['next_retry_at']
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertFalse(loop_engine.has_runnable_local_work(conn, repo_ids=['repo:example/backend'], config_result=self.config))
            item = conn.execute('SELECT work_item_id FROM work_items').fetchone()[0]
        card = board.get_work_item_detail(self.db, item)
        context = work_items.CommandContext('operator', 'owner', frozenset({'repo:example/backend'}), frozenset({'default'}))
        work_items.execute_command(self.db, item, context=context, command='retry',
            expected_version=card['version'], idempotency_key='budget-retry', notifier=lambda _: None)
        self.cycle(self.empty)
        with closing(sqlite3.connect(self.db)) as conn:
            child = conn.execute('SELECT task_id FROM tasks WHERE parent_task_id=?', (self.task,)).fetchone()
            self.assertIsNotNone(child)
            self.assertEqual(conn.execute('SELECT event_id FROM task_events WHERE task_id=? AND relationship=\'trigger\'', child).fetchone()[0], 'event:comment:pending')
            self.assertEqual(conn.execute('SELECT lifecycle FROM tasks WHERE task_id=?', (self.task,)).fetchone()[0], 'completed')

    def test_cancel_during_worktree_preparation_cannot_create_child(self):
        original = run_once._prepare_worktree
        def cancel(*args, **kwargs):
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE work_items SET canceled_at=?", (run_once._now(),))
                conn.execute("UPDATE workstreams SET lifecycle='canceled',active_task_id=NULL")
            return original(*args, **kwargs)
        with mock.patch.object(run_once, '_prepare_worktree', side_effect=cancel):
            self.cycle(self.empty)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
            self.assertFalse(loop_engine.has_runnable_local_work(conn, repo_ids=['repo:example/backend'], config_result=self.config))

    def test_revoked_pending_request_settles_without_creating_child(self):
        import yaml
        with mock.patch.object(run_once, '_prepare_worktree', side_effect=subprocess.TimeoutExpired(['git', 'fetch'], 1)):
            self.cycle(self.empty)
        due = self.state()[1]['next_retry_at']
        config = yaml.safe_load(self.fixture.config_path.read_text())
        config['repos'][0]['trusted_actors'] = ['different-user']
        self.fixture.config_path.write_text(yaml.safe_dump(config))
        with mock.patch.object(run_once, '_now', return_value=due):
            self.cycle(self.empty)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT lifecycle,active_task_id FROM workstreams').fetchone(), ('completed', None))
            self.assertEqual(conn.execute("SELECT binding_status FROM execution_subjects WHERE subject_id=?", (self.task,)).fetchone()[0], 'terminal')

    def test_completed_episode_owned_task_preserves_human_handoff(self):
        from robert_agent import execution_store
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tasks SET task_kind='ci_remediation',lifecycle='completed',metadata_json=json_set(metadata_json,'$.remediation_episode_id','episode')")
            conn.execute("DELETE FROM execution_subjects WHERE subject_type='task'")
            conn.execute("""INSERT INTO pr_remediation_episodes(episode_id,workstream_id,episode_kind,subject_key,
                observed_head_sha,observed_base_sha,status,first_seen_at,updated_at,last_task_id)
                SELECT 'episode',workstream_id,'ci','key','head','base','waiting_for_signal',created_at,updated_at,task_id FROM tasks""")
            execution_store.ensure_subject(conn, 'remediation_episode', 'episode', 'pr_remediation', run_once._now())
            conn.execute("UPDATE github_actions SET audit_status='accepted'")
            conn.execute("UPDATE worker_results SET metadata_json=json_set(metadata_json,'$.audit.status','accepted')")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertTrue(loop_engine.has_runnable_local_work(conn, repo_ids=['repo:example/backend'], config_result=self.config))
        self.cycle(self.empty)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT task_kind FROM tasks WHERE parent_task_id=?', (self.task,)).fetchone()[0], 'human_request')
            self.assertEqual(execution_store.get_binding(conn, 'remediation_episode', 'episode')['binding_status'], 'active')
