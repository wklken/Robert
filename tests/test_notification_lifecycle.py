"""Notifications must retain fresh requests after an assignment has completed."""
from contextlib import closing
import json
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

from robert_agent import discover, execution_store, github_scan, run_once, validate_config
from robert_agent.github_read import GitHubReadSession
from tests import test_board_control_integration as fixtures


class NotificationLifecycleTests(unittest.TestCase):
    def fixture(self, source_type='issue'):
        fixture = fixtures.BoardControlIntegrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        event = {'id': 11, 'number': 1, 'source_type': source_type, 'event_type': 'assigned',
                 'assigned_to': 'robot', 'assignment_actor_login': 'owner', 'actor_login': 'owner',
                 'title': 'Original work', 'body': 'Analyze old failure', 'state': 'open'}
        path = fixture.root / 'assignment.json'
        path.write_text(json.dumps({'events': [event]}))
        outcome = run_once.run_once(fixture.config_path, fixture_path=path, dry_run=True,
                                    skip_external=True, skip_publish=True)
        self.assertTrue(outcome['ok'], outcome)
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            task_id = conn.execute('SELECT task_id FROM tasks').fetchone()[0]
            conn.execute("UPDATE tasks SET lifecycle='completed'")
            conn.execute("UPDATE attempts SET status='completed',finished_at=?,metadata_json=?",
                         (run_once._now(), '{"result_contract":"compact-v1"}'))
            conn.execute("UPDATE workstreams SET lifecycle='completed',active_task_id=NULL")
            execution_store.close_binding(conn, 'task', task_id, run_once._now())
            conn.execute('UPDATE work_items SET completed_at=?', (run_once._now(),))
        return fixture

    def remote(self, source_type, comments, calls, *, timeline=None):
        assignment = {'id': 11, 'event': 'assigned', 'actor': {'login': 'owner'},
                      'assignee': {'login': 'robot'}, 'created_at': '2026-10-01T00:00:00Z'}
        source = {'number': 1, 'title': 'Original work', 'body': 'old requirement', 'state': 'open',
                  'updated_at': '2026-10-31T10:00:00Z', 'user': {'login': 'owner'},
                  'assignees': [{'login': 'robot'}], 'head': {'sha': 'head', 'ref': 'feature'},
                  'base': {'sha': 'base', 'ref': 'main', 'repo': {'full_name': 'example/repo'}}}

        def runner(args, **kwargs):
            self.assertEqual(args[:2], ['gh', 'api'], 'search quota is unavailable')
            endpoint = args[2]
            calls.append(endpoint)
            if endpoint.endswith('/timeline'):
                payload = [timeline if timeline is not None else [assignment]]
            elif endpoint.endswith('/issues/1/comments'):
                payload = [comments[:1], comments[1:]]
            elif endpoint.endswith('/pulls/1/reviews'):
                payload = [[{**comments[0], 'id': 32, 'submitted_at': comments[0]['created_at']}]]
            elif endpoint.endswith('/pulls/1/comments'):
                payload = [[{**comments[0], 'id': 33}]]
            elif endpoint.endswith(('/issues/1', '/pulls/1')):
                payload = source
            else:
                raise AssertionError(args)
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), '')

        return runner

    def scan(self, fixture, runner, source_type='issue', reason='mention'):
        config = validate_config.validate_config(fixture.config_path, skip_external=True)
        session = GitHubReadSession(fixture.db_path, account='robot', runner=runner)
        session.search_allowed = False
        hints = [{'repo_full_name': 'example/repo', 'id': 'thread', 'number': 1,
                  'source_type': source_type, 'event_type': 'notification',
                  'notification_reason': reason, 'event_at': '2026-10-31T10:00:00Z'}]
        batch = github_scan._scan_repo(config, config['repos'][0], session, hints)
        with closing(sqlite3.connect(fixture.db_path)) as conn:
            return json.loads(conn.execute('SELECT raw_events_json FROM github_scan_batches '
                                           'WHERE batch_id=?', (batch,)).fetchone()[0])

    def comments(self):
        return [{'id': identifier, 'user': {'login': actor}, 'body': body,
                 'created_at': f'2026-10-31T10:00:{index:02d}Z', 'author_association': 'OWNER'}
                for index, (identifier, actor, body) in enumerate([
                    (22, 'owner', '@robot analyze the new failure'),
                    (23, 'owner', '@robot also explain the second failure'),
                    (24, 'outsider', '@robot run an unrelated command'),
                    (25, 'robot', '@robot echo'),
                    (26, 'owner', 'An ordinary discussion comment'),
                ])]

    def consume(self, fixture):
        outcome = run_once.run_once(fixture.config_path, dry_run=True, skip_external=True,
                                    skip_publish=True, consume_observations=True)
        self.assertTrue(outcome['ok'], outcome)

    def test_search_floor_notification_preserves_all_requests_after_completed_assignment(self):
        for source_type in ('issue', 'pull_request'):
            with self.subTest(source_type=source_type):
                fixture = self.fixture(source_type)
                calls = []
                runner = self.remote(source_type, self.comments(), calls)
                raw = self.scan(fixture, runner, source_type)
                expected = {'comment:22', 'comment:23', 'comment:24'}
                if source_type == 'pull_request':
                    expected |= {'review:32', 'review_comment:33'}
                self.assertEqual({event['event_fingerprint'] for event in raw}, expected)
                self.assertIn('repos/example/repo/issues/1/comments', calls)
                self.consume(fixture)
                with closing(sqlite3.connect(fixture.db_path)) as conn:
                    events = {fingerprint: json.loads(payload) for fingerprint, payload in
                              conn.execute('SELECT event_fingerprint,payload_json FROM github_events')}
                    self.assertTrue(expected <= events.keys())
                    for fingerprint in expected - {'comment:24'}:
                        self.assertTrue(events[fingerprint]['drives_execution'])
                    self.assertFalse(events['comment:24']['drives_execution'])
                    self.assertEqual(conn.execute('SELECT count(*) FROM tasks').fetchone()[0], 2)
                self.scan(fixture, runner, source_type)
                self.consume(fixture)
                with closing(sqlite3.connect(fixture.db_path)) as conn:
                    self.assertEqual(conn.execute('SELECT count(*) FROM tasks').fetchone()[0], 2)

    def test_revoked_actor_cannot_drive_notification_after_historical_assignment(self):
        fixture = self.fixture()
        config = json.loads(fixture.config_path.read_text())
        config['repos'][0]['trusted_actors'] = ['new-owner']
        fixture.config_path.write_text(json.dumps(config))
        self.scan(fixture, self.remote('issue', self.comments(), []))
        self.consume(fixture)
        with closing(sqlite3.connect(fixture.db_path)) as conn:
            events = [json.loads(row[0]) for row in conn.execute(
                "SELECT payload_json FROM github_events WHERE event_fingerprint LIKE 'comment:%'")]
            self.assertEqual(len(events), 3)
            self.assertTrue(all(not event['drives_execution'] for event in events))
            self.assertEqual(conn.execute('SELECT count(*) FROM tasks').fetchone()[0], 1)

    def test_old_source_cache_cannot_hide_unobserved_comments(self):
        fixture = self.fixture()
        # Seed the exact pre-fix cache shape without relying on the old implementation.
        old = {'id': 11, 'number': 1, 'source_type': 'issue', 'event_type': 'notification',
               'event_fingerprint': 'assigned:11', 'assignment_actor_login': 'owner',
               'authorization_lookup_complete': True, 'trusted_trigger_found': True}
        runner = self.remote('issue', self.comments(), [])
        with patch.object(discover, 'SOURCE_CONTEXT_VERSION', 2):
            # source_events keys are independent of the loader's implementation.
            config = validate_config.validate_config(fixture.config_path, skip_external=True)
            hint = {'repo_full_name': 'example/repo', 'id': 'thread', 'number': 1,
                    'source_type': 'issue', 'event_type': 'notification',
                    'notification_reason': 'mention', 'event_at': '2026-10-31T10:00:00Z'}
            session = GitHubReadSession(fixture.db_path, account='robot', runner=runner)
            session.source_events(config['repos'][0], hint, lambda: [old],
                                  known_workstreams={'github:example/repo#1'}, notification_hints=[hint])
        raw = self.scan(fixture, runner)
        self.assertIn('comment:22', {event.get('event_fingerprint') for event in raw})


if __name__ == '__main__':
    unittest.main()
