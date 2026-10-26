from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import unittest
from unittest import mock

from tests import test_reliability as result_fixture
from tests import test_run_once as pipeline_fixture
from robert_agent import common, execution_store, run_once
from robert_agent.worker import result


class TimeoutIsolationTests(unittest.TestCase):
    def environment(self, remaining=None):
        env = dict(os.environ)
        env.pop('_ROBERT_CONTROL_DEADLINE', None)
        if remaining is not None:
            env['_ROBERT_CONTROL_DEADLINE'] = str(time.monotonic() + remaining)
        return mock.patch.dict(os.environ, env, clear=True)

    def fixture(self):
        fixture = pipeline_fixture.RunOnceDryRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_unbounded_command_does_not_gain_an_implicit_sixty_second_cap(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
        with self.environment():
            common.run_command(['git', 'fetch'], runner=runner)
        self.assertIsNone(runner.call_args.kwargs.get('timeout'))

    def test_remaining_budget_timeout_is_distinct_from_command_cap(self):
        for remaining, expected in ((100, 'TimeoutExpired'), (1, 'ControlDeadlineExceeded')):
            with self.subTest(remaining=remaining), self.environment(remaining):
                runner = mock.Mock(side_effect=subprocess.TimeoutExpired(['git', 'status'], 5))
                with self.assertRaises(subprocess.TimeoutExpired) as caught:
                    common.run_command(['git', 'status'], runner=runner, timeout=5)
                self.assertEqual(type(caught.exception).__name__, expected)

    def test_slow_attempt_inspection_does_not_starve_discovery(self):
        fixture = self.fixture()
        with self.environment():
            self.assertTrue(run_once.run_once(fixture.config_path, fixture_path=fixture.fixture_path,
                                             dry_run=True, skip_external=True)['ok'])
            db = fixture.data_dir / 'dd.sqlite3'
            now = datetime.now(timezone.utc).isoformat()
            with closing(sqlite3.connect(db)) as conn, conn:
                old_attempt, old_task = conn.execute('SELECT attempt_id,task_id FROM attempts').fetchone()
                conn.execute("UPDATE attempts SET status='running',started_at=?,heartbeat_at=?,worktree_path=?",
                             (now, now, str(fixture.repo_root)))
                conn.execute("UPDATE tasks SET lifecycle='running'")
            events = json.loads(fixture.fixture_path.read_text())['events']
            events.append({**events[0], 'id': 'independent-comment', 'number': 124})
            fixture.fixture_path.write_text(json.dumps({'events': events}))
            with mock.patch.object(run_once, '_dispatch_process_status', return_value={'status': 'dead'}), \
                    mock.patch.object(run_once, '_run_inspection_command', side_effect=subprocess.TimeoutExpired(['git', 'status'], 5)):
                outcome = run_once.run_once(fixture.config_path, fixture_path=fixture.fixture_path,
                                            dry_run=True, skip_external=True)
            self.assertTrue(outcome['ok'], outcome)
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute('SELECT status FROM attempts WHERE attempt_id=?', (old_attempt,)).fetchone()[0], 'running')
                self.assertEqual(conn.execute('SELECT lifecycle FROM tasks WHERE task_id=?', (old_task,)).fetchone()[0], 'running')
                self.assertEqual(conn.execute('SELECT count(*) FROM tasks').fetchone()[0], 2)

    def test_audit_scope_timeout_defers_result_without_rejecting_it(self):
        fixture = result_fixture.DurableResultTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.assertTrue(result.record_result(fixture.db, fixture.payload)['ok'])
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            task, repo = conn.execute('SELECT t.task_id,w.repo_id FROM tasks t JOIN workstreams w ON w.workstream_id=t.workstream_id').fetchone()
            execution_store.ensure_subject(conn, 'task', task, 'human_request', now)
            conn.execute('''INSERT INTO route_decisions(route_decision_id,task_id,route_id,expected_output,confidence,created_at)
                VALUES ('route-scope',?,'comment-analysis','comment_analysis','high',?)''', (task, now))
            with mock.patch.object(run_once, '_action_scope_for_result', side_effect=subprocess.TimeoutExpired(['git', 'rev-parse'], 5)):
                count = run_once._audit_completed_results(conn, repo, Path(fixture.db).parent,
                    fixture.db, {}, 'run-unused', now, True, [], {'remaining': 3})
            self.assertEqual(count, 0)
            self.assertNotIn('audit', json.loads(conn.execute('SELECT metadata_json FROM worker_results').fetchone()[0]))
            self.assertEqual(conn.execute('SELECT lifecycle FROM tasks').fetchone()[0], 'running')

    def test_route_command_timeout_does_not_block_an_independent_event(self):
        fixture = self.fixture()
        events = [{'id': f'review-{number}', 'number': number, 'source_type': 'pull_request',
                   'event_type': 'review_request', 'actor_login': 'wklken',
                   'requested_reviewer': 'robert-bot', 'title': 'Review'} for number in (22, 23)]
        fixture.fixture_path.write_text(json.dumps({'events': events}))
        original = run_once._prepare_worktree
        visited = []

        def prepare(repo, event, route, dry_run):
            visited.append(event['number'])
            if event['number'] == 22:
                raise subprocess.TimeoutExpired(['git', 'fetch'], 5)
            return original(repo, event, route, dry_run)

        with self.environment(), mock.patch.object(run_once, '_prepare_worktree', side_effect=prepare):
            outcome = run_once.run_once(fixture.config_path, fixture_path=fixture.fixture_path,
                                       dry_run=True, skip_external=True)
        self.assertEqual(visited, [22, 23])
        self.assertEqual(len(outcome['prompt_paths']), 1)
        self.assertEqual(outcome['repo_summaries'][0]['failure_status'], 'failed_route')

    def test_shared_deadline_stops_routing_instead_of_becoming_an_event_failure(self):
        fixture = self.fixture()
        with mock.patch.object(run_once, '_prepare_worktree', side_effect=common.ControlDeadlineExceeded(['git', 'fetch'], 1)):
            outcome = run_once.run_once(fixture.config_path, fixture_path=fixture.fixture_path,
                                       dry_run=True, skip_external=True)
        self.assertEqual(outcome['repo_summaries'][0]['failure_status'], 'execution_deadline')
        self.assertEqual(outcome['prompt_paths'], [])

    def test_budget_can_expire_while_a_shorter_command_cap_is_in_effect(self):
        runner = mock.Mock(side_effect=subprocess.TimeoutExpired(['git', 'status'], 5, output='partial', stderr='diagnostic'))
        with mock.patch.dict(os.environ, {'_ROBERT_CONTROL_DEADLINE': '100'}), \
                mock.patch.object(common.time, 'monotonic', side_effect=[10, 101]):
            with self.assertRaises(common.ControlDeadlineExceeded) as caught:
                common.run_command(['git', 'status'], runner=runner, timeout=5)
        self.assertEqual(caught.exception.output, 'partial')
        self.assertEqual(caught.exception.stderr, 'diagnostic')
