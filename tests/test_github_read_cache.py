from tests import support  # noqa: F401
from contextlib import closing
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from robert_agent import discover, storage


REPO = {'full_name': 'example/backend', 'github_account': 'robert-bot', 'trusted_actors': ['wklken']}


class GitHubReadCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'state.sqlite3'
        storage.init_database(self.db)

    def session(self, runner, **kwargs):
        from robert_agent.github_read import GitHubReadSession
        return GitHubReadSession(self.db, account='robert-bot', runner=runner, now=lambda: 1000, **kwargs)

    def test_successful_reads_are_copied_and_failures_are_retried(self):
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0, '{"state":"open"}', '')
        session = self.session(runner)
        args = ['gh', 'api', 'repos/example/backend/issues/7']
        with self.assertRaises(subprocess.CalledProcessError):
            session(args, check=True)
        result = session(args, check=True)
        result.stdout = 'changed'
        self.assertEqual(session(args, check=True).stdout, '{"state":"open"}')
        self.assertEqual(len(calls), 2)
        self.assertEqual(session.metrics()['cache_hits'], 1)

    def test_notifications_304_replays_hints_and_preserves_marker(self):
        calls = []
        notification = {'id': 'n1', 'repository': {'full_name': 'example/backend'},
                        'subject': {'type': 'PullRequest', 'url': 'https://api.github.com/repos/example/backend/pulls/7'},
                        'updated_at': '2026-10-28T00:00:00Z'}
        def runner(args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                return subprocess.CompletedProcess(args, 0, 'HTTP/2.0 200 OK\r\nLast-Modified: stamp\r\nX-Poll-Interval: 60\r\n\r\n' + json.dumps([notification]), '')
            return subprocess.CompletedProcess(args, 1, 'HTTP/2.0 304 Not Modified\r\nX-Poll-Interval: 90\r\n\r\n', 'HTTP304')
        first = self.session(runner).collect_notifications([REPO])
        second_session = self.session(runner)
        self.assertEqual(first, second_session.collect_notifications([REPO]))
        self.assertIn('If-Modified-Since: stamp', calls[-1])
        self.assertEqual(second_session.poll_interval_seconds, 90)

    def test_failed_page_does_not_advance_notification_marker(self):
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                return subprocess.CompletedProcess(args, 0, 'HTTP/2.0 200 OK\nLast-Modified: stamp\nLink: <https://api.github.com/notifications?page=2>; rel="next"\n\n[]', '')
            raise subprocess.CalledProcessError(1, args)
        with self.assertRaises(subprocess.CalledProcessError):
            self.session(runner).collect_notifications([REPO])
        with closing(__import__('sqlite3').connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM github_read_cache').fetchone()[0], 0)

    def test_source_cache_is_replayed_copied_and_context_invalidates(self):
        session = self.session(lambda *_a, **_k: None)
        raw = {'number': 7, 'source_type': 'pull_request', 'event_type': 'mention', 'event_at': 'v1'}
        count = []
        def load():
            count.append(1)
            return [{'id': 12, 'body': 'hello', 'authorization_lookup_complete': True}]
        kwargs = {'known_workstreams': set(), 'review_author_allowlist': []}
        first = session.source_events(REPO, raw, load, **kwargs)
        first[0]['body'] = 'mutated'
        self.assertEqual(session.source_events(REPO, raw, load, **kwargs)[0]['body'], 'hello')
        session.source_events(REPO, raw, load, known_workstreams={'github:example/backend!7'})
        self.assertEqual(len(count), 2)

    def test_incomplete_source_results_are_not_cached_and_empty_results_are(self):
        session = self.session(lambda *_a, **_k: None)
        raw = {'number': 7, 'source_type': 'issue', 'event_type': 'notification', 'event_at': 'v1'}
        count = []
        def load():
            count.append(1)
            return [{'authorization_lookup_complete': False}] if len(count) == 1 else []
        self.assertEqual(len(session.source_events(REPO, raw, load)), 1)
        self.assertEqual(session.source_events(REPO, raw, load), [])
        self.assertEqual(session.source_events(REPO, raw, load), [])
        self.assertEqual(len(count), 2)

    def test_notification_pagination_and_marker_commit(self):
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                return subprocess.CompletedProcess(args, 0, 'HTTP/2.0 200 OK\nLast-Modified: stamp\nLink: <https://api.github.com/notifications?page=2>; rel="next"\n\n[]', '')
            return subprocess.CompletedProcess(args, 0, 'HTTP/2.0 200 OK\n\n[]', '')
        self.assertEqual(self.session(runner).collect_notifications([REPO]), {})
        with closing(__import__('sqlite3').connect(self.db)) as conn:
            payload = json.loads(conn.execute('SELECT payload_json FROM github_read_cache').fetchone()[0])
        self.assertEqual(payload['last_modified'], 'stamp')
        self.assertEqual(len(calls), 2)

    def test_discovery_replays_full_events_and_refreshes_on_new_notification(self):
        item = {'id': 1, 'number': 7, 'title': 'Fix', 'body': '', 'isPullRequest': True,
                'author': {'login': 'wklken'}, 'authorAssociation': 'COLLABORATOR',
                'updatedAt': '2026-10-28T00:00:00Z', 'url': 'https://github.com/example/backend/pull/7'}
        hint = {'id': 'n1', 'repo_full_name': 'example/backend', 'number': 7,
                'source_type': 'pull_request', 'event_type': 'notification', 'event_at': 'v1'}
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            if args[1] == 'search':
                payload = [item]
            elif args[2].endswith('/timeline'):
                payload = [[{'id': 30, 'event': 'assigned', 'actor': {'login': 'someone'},
                             'assignee': {'login': 'robert-bot'}, 'created_at': '2026-10-28T00:00:00Z'}]]
            elif args[2].endswith('/comments') or args[2].endswith('/reviews'):
                payload = [[]]
            elif '/pulls/' in args[2]:
                payload = {'user': {'login': 'wklken'}, 'base': {'ref': 'main'},
                           'head': {'ref': 'fix', 'sha': 'a'}}
            else:
                payload = {'state': 'open', 'user': {'login': 'wklken'},
                           'updated_at': '2026-10-28T00:00:00Z'}
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), '')
        kwargs = {'known_workstreams': {'github:example/backend!7'},
                  'notification_hints': [hint], 'include_notifications': False}
        cold = self.session(runner).collect_events(REPO, **kwargs)
        self.assertEqual(len(calls), 8)  # Original identical workload used18.
        warm_session = self.session(runner)
        self.assertEqual(warm_session.collect_events(REPO, **kwargs), cold)
        self.assertEqual(len(calls), 8)
        cold[0]['body'] = 'mutated after delivery'
        self.assertNotEqual(warm_session.collect_events(REPO, **kwargs)[0]['body'], cold[0]['body'])
        hint['event_at'] = 'v2'
        self.session(runner).collect_events(REPO, **kwargs)
        self.assertEqual(len(calls), 16)

    def test_failed_detail_read_is_retried_even_with_same_notification(self):
        hint = {'id': 'n1', 'repo_full_name': 'example/backend', 'number': 7,
                'source_type': 'issue', 'event_type': 'notification', 'event_at': 'v1'}
        failures = [True]
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            if args[1] == 'search':
                payload = []
            elif args[2].endswith('/issues/7'):
                if failures[0]:
                    raise subprocess.CalledProcessError(1, args)
                payload = {'state': 'open', 'user': {'login': 'wklken'}}
            elif args[2].endswith('/timeline'):
                payload = [[{'id': 30, 'event': 'assigned', 'actor': {'login': 'wklken'},
                             'assignee': {'login': 'robert-bot'}}]]
            else:
                payload = [[]]
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), '')
        kwargs = {'notification_hints': [hint], 'include_notifications': False}
        discover.collect_live_events(REPO, read_session=self.session(runner), **kwargs)
        failures[0] = False
        events = discover.collect_live_events(REPO, read_session=self.session(runner), **kwargs)
        self.assertEqual(events[0]['assignment_actor_login'], 'wklken')
        self.assertEqual(sum(a[2].endswith('/issues/7') for a in calls), 2)

    def test_expired_and_configuration_changed_observations_refresh(self):
        clock = [1000]
        from robert_agent.github_read import GitHubReadSession
        session = GitHubReadSession(self.db, account='robert-bot', now=lambda: clock[0])
        raw = {'number': 7, 'source_type': 'issue', 'event_type': 'mention', 'event_at': 'v1'}
        calls = []
        def load():
            calls.append(1)
            return []
        session.source_events(REPO, raw, load)
        session.source_events({**REPO, 'trusted_actors': ['other']}, raw, load)
        clock[0] += 1801
        session.source_events({**REPO, 'trusted_actors': ['other']}, raw, load)
        self.assertEqual(len(calls), 3)

    def test_invalid_json_is_not_a_successful_source_observation(self):
        session = self.session(lambda args, **kwargs: subprocess.CompletedProcess(args, 0, 'badJSON', ''))
        raw = {'number': 7, 'source_type': 'issue', 'event_type': 'mention', 'event_at': 'v1'}
        count = []
        def load():
            count.append(1)
            discover._try_run_json(['gh', 'api', 'repos/example/backend/issues/7'], runner=session)
            return []
        session.source_events(REPO, raw, load)
        session.source_events(REPO, raw, load)
        self.assertEqual(len(count), 2)

    def test_host_routing_and_account_cache_isolation(self):
        from robert_agent.github_read import GitHubReadSession
        calls = []
        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, '[]', '')
        session = GitHubReadSession(self.db, account='bot', host='enterprise.example', runner=runner)
        session(['gh', 'api', 'repos/example/backend/issues'], check=True)
        self.assertEqual(calls[-1][0][-2:], ['--hostname', 'enterprise.example'])
        session(['gh', 'search', 'issues', '--repo', 'example/backend'])
        self.assertEqual(calls[-1][1]['env']['GH_HOST'], 'enterprise.example')
        raw = {'number': 7, 'source_type': 'issue', 'event_type': 'mention'}
        session.source_events(REPO, raw, lambda: [{'body': 'private'}])
        other = GitHubReadSession(self.db, account='other', host='enterprise.example', runner=runner)
        self.assertEqual(other.source_events(REPO, raw, lambda: []), [])

    def test_notifications_for_other_repo_do_not_invalidate_current_search(self):
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, '[]', '')
        one = self.session(runner)
        one.collect_events(REPO, notification_hints=[], include_notifications=False)
        two = self.session(runner)
        two.collect_events(REPO, notification_hints=[{
            'id': 'unrelated', 'repo_full_name': 'other/repo', 'number': 7,
            'source_type': 'issue', 'event_type': 'notification', 'event_at': 'v1',
        }], include_notifications=False)
        # Mismatched preloaded hints must not influence this repo's search cache.
        self.assertEqual(len(calls), 2)

    def test_notification_throttle_preserves_server_backoff(self):
        def runner(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, 'HTTP/2.0 429 Too Many Requests\nRetry-After: 180\nX-Poll-Interval: 60\n\n{}', '')
        session = self.session(runner)
        with self.assertRaises(subprocess.CalledProcessError):
            session.collect_notifications([REPO])
        self.assertEqual(session.poll_interval_seconds, 180)
