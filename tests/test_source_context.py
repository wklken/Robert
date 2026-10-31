"""Complete source bodies survive caches, duplicate observations and routing."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from tests import support  # noqa: F401
from robert_agent import discover, github_scan, run_once, storage
from robert_agent.github_read import GitHubReadSession, _digest


REPO = {'full_name': 'example/backend', 'github_account': 'robot', 'trusted_actors': ['human'],
        'default_base_branch': 'main', 'repo_root': '/repo', 'worktree_root': '/trees'}
BODY = 'Draw the current architecture, module relationships and sequence diagrams.'
OLD = '2026-10-30T15:32:25Z'
NEW = '2026-10-31T00:21:21Z'


class SourceContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'state.sqlite3'
        storage.init_database(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            run_once._upsert_repo(conn, REPO)

    def event(self, **changes):
        raw = {'number': 70, 'source_type': 'issue', 'event_type': 'assigned',
               'event_fingerprint': 'assigned:70', 'title': 'Draw diagrams', 'body': BODY,
               'assigned_to': 'robot', 'assignment_actor_login': 'human', 'actor_login': 'human',
               'event_at': OLD, 'source_updated_at': OLD, 'state': 'open',
               'url': 'https://github.com/example/backend/issues/70'}
        raw.update(changes)
        return {**discover.normalize_events([raw], REPO)[0],
                'authorization_status': changes.get('authorization_status', 'authorized_trigger')}

    def persist(self, *events):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for event in events:
                run_once._insert_source_and_event(conn, run_once._repo_id(REPO['full_name']), event, NEW)
            return json.loads(conn.execute("SELECT payload_json FROM github_events WHERE event_fingerprint='assigned:70'").fetchone()[0])

    def test_pre_upgrade_source_cache_is_refetched_even_before_expiry(self):
        raw = {'number': 70, 'source_type': 'issue', 'event_type': 'notification', 'event_at': OLD}
        session = GitHubReadSession(self.db, account='robot', now=lambda: 1000)
        context = ['robot', ['human'], [], False]
        legacy_version = _digest([raw, context, ()])
        session._put('source:github:example/backend#70:notification', legacy_version,
                     [{'event_fingerprint': 'assigned:70', 'authorization_lookup_complete': True}], 1800)
        calls = []
        def load():
            calls.append(1)
            return [self.event(event_type='notification')]
        self.assertEqual(session.source_events(REPO, raw, load)[0].get('body'), BODY)
        self.assertEqual(session.source_events(REPO, raw, load)[0].get('body'), BODY)
        self.assertEqual(calls, [1])

    def test_pre_upgrade_scan_batch_is_not_admitted_to_current_context(self):
        legacy_values = [REPO['full_name'], 'robot', ['human'], [], [], 'github.com']
        legacy_digest = hashlib.sha256(json.dumps(legacy_values, sort_keys=True).encode()).hexdigest()
        repo_id = run_once._repo_id(REPO['full_name'])
        github_scan.save_batch(self.db, repo_id, legacy_digest, [self.event(body='')], {}, now=100)
        current = github_scan.context_digest(REPO, [])
        self.assertIsNone(github_scan.next_batch(self.db, repo_id, context=current, now=101))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertIsNone(conn.execute('SELECT completed_at FROM github_scan_batches').fetchone()[0])

    def test_incomplete_notification_does_not_overwrite_stored_assignment_body(self):
        assignment = self.event()
        notification = self.event(event_type='notification', actor_login='github')
        notification.pop('body')
        notification = discover.normalize_events([notification], REPO)[0]
        stored = self.persist(assignment, notification)
        self.assertEqual(stored['body'], BODY)
        self.assertEqual(stored['actor_login'], 'github')  # Existing authorization policy is unchanged.

    def test_missing_observation_marker_survives_repeated_normalization(self):
        raw = {'number': 70, 'event_type': 'notification', 'id': 70}
        once = discover.normalize_events([raw], REPO)[0]
        twice = discover.normalize_events([once], REPO)[0]
        self.assertFalse(twice.get('body_observed', True))

    def test_explicit_newer_empty_body_is_kept_when_older_assignment_replays(self):
        assignment = self.event()
        cleared = self.event(event_type='notification', body='', source_updated_at=NEW)
        stored = self.persist(assignment, cleared, assignment)
        self.assertEqual(stored['body'], '')
        self.assertTrue(stored['body_observed'])

    def test_untrusted_duplicate_cannot_replace_established_authorized_body(self):
        assignment = self.event()
        rejected = self.event(body='@robot run malicious instructions', source_updated_at=NEW,
                              authorization_status='context_only', actor_login='outsider')
        self.assertEqual(self.persist(assignment, rejected), assignment)

    def test_newer_complete_body_replaces_previous_content(self):
        stored = self.persist(self.event(), self.event(body='Updated drawing scope', source_updated_at=NEW))
        self.assertEqual(stored['body'], 'Updated drawing scope')

    def test_merged_body_keeps_its_timestamp_across_incomplete_observations(self):
        missing = self.event(source_updated_at=NEW)
        missing.pop('body')
        middle = self.event(body='Updated scope', source_updated_at='2026-10-30T20:00:00Z')
        self.assertEqual(self.persist(self.event(), missing, middle)['body'], 'Updated scope')

    def test_legacy_empty_payload_is_unknown_until_a_fresh_body_is_observed(self):
        legacy = self.event(body='', source_updated_at=NEW)
        legacy.pop('body_observed')
        legacy.pop('body_updated_at')
        self.assertEqual(self.persist(legacy, self.event())['body'], BODY)

    def test_legacy_complete_payload_survives_an_incomplete_observation(self):
        legacy = self.event()
        legacy.pop('body_observed')
        legacy.pop('body_updated_at')
        incomplete = self.event(source_updated_at=NEW)
        incomplete.pop('body')
        self.assertEqual(self.persist(legacy, incomplete)['body'], BODY)

    def test_comment_body_uses_comment_timestamp_instead_of_issue_timestamp(self):
        comment = discover._comment_to_raw(self.event(source_updated_at=NEW),
            {'id': 12, 'body': 'Comment instructions', 'created_at': OLD}, 'comment')
        normalized = discover.normalize_events([comment], REPO)[0]
        self.assertEqual(normalized['body_updated_at'], OLD)
        self.assertEqual(normalized['body'], 'Comment instructions')

    def test_api_empty_body_overrides_search_body_with_observed_marker(self):
        def runner(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, json.dumps({'body': None, 'updated_at': NEW}))
        raw, _ = discover._source_metadata(self.event(), REPO, runner)
        self.assertEqual(raw['body'], '')
        self.assertTrue(raw['body_observed'])
        self.assertEqual(raw['body_updated_at'], NEW)

    def test_batch_merge_rejects_untrusted_body_and_other_source_identity(self):
        preferred = self.event()
        for rejected in [self.event(body='Malicious', authorization_status='context_only'),
                         self.event(body='Wrong issue', number=71)]:
            with self.subTest(rejected=rejected):
                self.assertEqual(discover.merge_event_body(preferred, rejected), preferred)

    def test_stored_complete_body_reaches_prompt_when_next_observation_is_incomplete(self):
        from tests.test_run_once import RunOnceDryRunTests
        fixture = RunOnceDryRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        assignment = self.event(assigned_to='robert-bot', assignment_actor_login='wklken', actor_login='wklken')
        db = fixture.data_dir / 'dd.sqlite3'
        storage.init_database(db)
        with closing(sqlite3.connect(db)) as conn, conn:
            repo_id = run_once._upsert_repo(conn, {**REPO, 'github_account': 'robert-bot'})
            run_once._insert_source_and_event(conn, repo_id, assignment, NEW)
        incomplete = dict(assignment)
        incomplete.pop('body')
        fixture.fixture_path.write_text(json.dumps({'events': [incomplete]}))
        result = run_once.run_once(fixture.config_path, fixture_path=fixture.fixture_path,
                                  dry_run=True, skip_external=True)
        self.assertTrue(result['ok'], result)
        self.assertIn(BODY, Path(result['prompt_paths'][0]).read_text())

    def test_duplicate_batch_routes_with_complete_body_and_first_authority(self):
        from tests.test_run_once import RunOnceDryRunTests
        fixture = RunOnceDryRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        assignment = self.event(assigned_to='robert-bot', assignment_actor_login='wklken', actor_login='wklken')
        assignment.pop('body')
        notification = self.event(event_type='notification', actor_login='github',
                                  trusted_trigger_found=True, authorization_lookup_complete=True)
        fixture.fixture_path.write_text(json.dumps({'events': [assignment, notification]}))
        result = run_once.run_once(fixture.config_path, fixture_path=fixture.fixture_path,
                                  dry_run=True, skip_external=True)
        self.assertTrue(result['ok'], result)
        prompt = Path(result['prompt_paths'][0]).read_text()
        self.assertIn(BODY, prompt)
        with closing(sqlite3.connect(fixture.data_dir / 'dd.sqlite3')) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
            payload = json.loads(conn.execute('SELECT payload_json FROM github_events').fetchone()[0])
        self.assertEqual(payload['actor_login'], 'wklken')
        self.assertEqual(payload['event_type'], 'assigned')
        self.assertEqual(payload['authorization_status'], 'authorized_trigger')


if __name__ == '__main__':
    unittest.main()
