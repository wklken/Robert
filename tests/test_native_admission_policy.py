"""Unstarted rounds must satisfy current GitHub trust and route configuration."""
from contextlib import closing
import sqlite3
import unittest
import yaml
from tests import test_review_coordinator as review_fixtures, test_vote_coordinator as vote_fixtures


class NativeAdmissionPolicyTests(unittest.TestCase):
    def fixture(self, vote):
        if vote:
            owner = vote_fixtures.VoteCoordinatorTests()
            owner.setUp()
            self.addCleanup(owner.doCleanups)
            return owner.fixture, owner.event
        owner = review_fixtures.ReviewCoordinatorTests()
        owner.setUp()
        self.addCleanup(owner.doCleanups)
        return owner, owner.event

    def test_unstarted_review_and_vote_respect_revoked_trust_and_disabled_routes(self):
        for vote in (False, True):
            for change in ('trust', 'disabled'):
                with self.subTest(vote=vote, change=change):
                    fixture, event = self.fixture(vote)
                    fixture.cycle([event()])
                    first, _ = fixture.current()
                    fixture.cycle([event('second')])
                    config = yaml.safe_load(fixture.config_path.read_text())
                    if change == 'trust':
                        config['repos'][0]['trusted_actors'] = ['replacement-maintainer']
                    else:
                        config['routes']['vote' if vote else 'review-pr']['multi_agent_vote' if vote else 'multi_agent_review']['enabled'] = False
                    fixture.config_path.write_text(yaml.safe_dump(config))
                    fixture.finish_task(first)
                    fixture.cycle([])
                    with closing(sqlite3.connect(fixture.db)) as conn:
                        self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
                        table = 'vote_sessions' if vote else 'review_sessions'
                        row = conn.execute(f"SELECT status,error FROM {table} WHERE trigger_fingerprint='comment:second'").fetchone()
                        self.assertEqual(row[0], 'canceled')
                        self.assertIn('current', row[1])

    def test_prepared_native_round_rechecks_trust_at_dispatch(self):
        from unittest import mock
        from robert_agent import run_once, validate_config
        for vote in (False, True):
            with self.subTest(vote=vote):
                fixture, event = self.fixture(vote)
                fixture.cycle([event()])
                config = yaml.safe_load(fixture.config_path.read_text())
                config['repos'][0]['trusted_actors'] = ['replacement-maintainer']
                fixture.config_path.write_text(yaml.safe_dump(config))
                current = validate_config.validate_config(fixture.config_path, skip_external=True)
                repo = current['repos'][0]
                queue = []
                with closing(sqlite3.connect(fixture.db)) as conn:
                    run_once._queue_prepared_attempts(conn, 'repo:example/backend', repo, queue, 3)
                self.assertTrue(queue)
                with mock.patch.object(run_once.dispatch, 'dispatch_worker') as launch:
                    run_once._dispatch_queued(fixture.db, ['repo:example/backend'], queue, True, 3,
                        current['workers'], current['default_worker'], current.get('route_worker_models'), current)
                    launch.assert_not_called()
                with closing(sqlite3.connect(fixture.db)) as conn:
                    self.assertEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'canceled')
