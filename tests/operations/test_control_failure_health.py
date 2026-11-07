from contextlib import closing
import json
import sqlite3
import unittest
from unittest import mock
from tests import support  # noqa: F401
from tests.support import reconcile_isolation as fixtures
from robert_agent.execution import runtime_state as daemon_state
from robert_agent.operations import health as reliability_health


class ReviewHealthTests(unittest.TestCase):
    setUp = fixtures.ControlFailureHealthFixture.setUp
    event = fixtures.ControlFailureHealthFixture.event

    def test_non_crash_failures_are_counted_per_lane(self):
        for lane in ('scan', 'local'):
            for index, kind in enumerate(('child_timeout', 'partial_failure', 'repository_pipeline_failed')):
                with closing(daemon_state.connect(self.db)) as conn, conn:
                    daemon_state.record_event(conn, 'daemon', kind, 'failed', {'control_lane': lane},
                        now=f'2026-10-28T11:{index:02d}:00+00:00')
        self.assertEqual(reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])['control_failure_streak'], 3)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications WHERE notification_type='control_plane_failed'").fetchone()[0], 2)

    def test_writer_contention_does_not_drop_health_measurements(self):
        for minute in range(3):
            self.event('child_failed', 'failed', minute)
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            health = reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])
            self.assertGreater(health['database_bytes'], 0)
            self.assertEqual(health['control_failure_streak'], 3)
            self.assertFalse(health['alerts_persisted'])
        self.assertTrue(reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])['alerts_persisted'])

    def test_alert_write_contention_retries_at_local_poll_interval(self):
        from tests.support import retention as health_fixtures
        from robert_agent.runtime import daemon
        fixture = health_fixtures.RetentionFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with mock.patch.object(daemon.retention, 'prune_database', return_value={
                'ok': True, 'status': 'pruned', 'counts': {}}), \
             mock.patch.object(reliability_health, 'collect', return_value={'alerts_persisted': False}), \
             mock.patch.object(daemon.time, 'monotonic', return_value=100):
            daemon._maybe_prune(fixture.context, now=self.now)
        expected = 100 + fixture.context.daemon_config['local_poll_seconds']
        self.assertEqual(fixture.context.next_health_at, expected)
        self.assertEqual(fixture.context.next_maintenance_at, expected)

class ControlFailureHealthTests(fixtures.ControlFailureHealthFixture):
    def test_healthy_local_drains_do_not_hide_failed_scans(self):
        for minute in range(3):
            self.event('child_failed', 'failed', minute * 2)
            self.event('local_drain_completed', 'ok', minute * 2 + 1)
        self.assertEqual(reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])['control_failure_streak'], 3)

    def test_many_local_successes_do_not_resolve_scan_failure(self):
        from datetime import datetime, timedelta, timezone
        for minute in range(3):
            self.event('child_failed', 'failed', minute)
        reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])
        with closing(daemon_state.connect(self.db)) as conn, conn:
            for index in range(110):
                when = (datetime(2026, 10, 28, 11, 10, tzinfo=timezone.utc) + timedelta(seconds=index)).isoformat()
                daemon_state.record_event(conn, 'daemon', 'local_drain_completed', 'ok', now=when)
        self.assertEqual(reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])['control_failure_streak'], 3)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM notifications WHERE notification_type='control_plane_failed'").fetchone()[0], 'recorded')

    def test_interrupted_executors_alert_without_a_daemon_failure_event(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for minute in range(3):
                conn.execute("INSERT INTO agent_runs(run_id,status,started_at,finished_at,config_path,dry_run,error_json) VALUES (?,'failed',?,?,'/config',0,?)",
                    ('interrupted-' + str(minute), f'2026-10-28T11:{minute:02d}:00Z', f'2026-10-28T11:{minute:02d}:30Z',
                     json.dumps({'status': 'executor_interrupted'})))
        self.assertEqual(reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])['control_failure_streak'], 3)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications WHERE notification_type='control_plane_failed'").fetchone()[0], 1)

    def test_repeated_failures_alert_once_until_success_and_can_alert_again(self):
        for minute in range(3):
            self.event('child_failed', 'failed', minute)
        for _ in range(2):
            health = reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])
            self.assertEqual(health['control_failure_streak'], 3)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications WHERE notification_type='control_plane_failed'").fetchone()[0], 1)
        self.event('live_poll_completed', 'ok', 3)
        self.assertEqual(reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])['control_failure_streak'], 0)
        self.event('child_failed', 'failed', 4)
        self.event('child_failed', 'failed', 5)
        self.assertEqual(reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])['control_failure_streak'], 2)
        self.event('child_failed', 'failed', 6)
        reliability_health.collect(self.db, now=self.now, repo_ids=['repo'])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications WHERE notification_type='control_plane_failed'").fetchone()[0], 2)
