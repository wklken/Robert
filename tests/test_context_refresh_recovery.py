from tests import support  # noqa: F401
from contextlib import closing
import json
import sqlite3
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock

from robert_agent import daemon, github_scan, run_once, storage, validate_config, wakeup
from tests import test_retention as retention_fixtures, test_run_once as run_fixtures


class ContextRefreshRecoveryTests(unittest.TestCase):
    def context(self):
        fixture = retention_fixtures.RetentionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        context = fixture.context
        context.startup_live_poll_pending = False
        context.next_live_poll_at = '2099-01-01T00:00:00+00:00'
        context.background_lanes = {
            name: SimpleNamespace(process=None, start=mock.Mock(), poll=mock.Mock(return_value=None))
            for name in ('scan', 'publish')}
        return fixture, context, context.background_lanes['scan']

    def decide(self, context, monotonic=100):
        with mock.patch.object(daemon, '_maybe_prune'), \
             mock.patch.object(daemon.time, 'monotonic', return_value=monotonic), \
             mock.patch.object(daemon.loop_engine, 'has_runnable_local_work', return_value=False):
            return daemon.run_once_decision(context, now=retention_fixtures.NOW)

    def request(self, fixture, **kwargs):
        with fixture.connect() as conn:
            result = wakeup.request_wakeup(conn, repo_id='repo', reason='manual_operator_request',
                dedupe_key='refresh', metadata={'kind': 'github_context_refresh'},
                now=retention_fixtures.NOW, **kwargs)
        return result

    def test_refresh_retry_gate_reopens_without_changing_notification_schedule(self):
        fixture, context, scan = self.context()
        self.request(fixture)
        context.context_refresh_retry_at = 105
        self.decide(context, monotonic=104)
        scan.start.assert_not_called()
        self.decide(context, monotonic=105)
        self.assertIn('--refresh-context', scan.start.call_args.args[0])
        self.assertEqual(context.next_live_poll_at, '2099-01-01T00:00:00+00:00')
        self.assertTrue(context.scan_context_refresh)

    def test_due_notification_poll_takes_priority_over_refresh_gate(self):
        fixture, context, scan = self.context()
        self.request(fixture)
        context.context_refresh_retry_at = 600
        context.next_live_poll_at = retention_fixtures.NOW
        self.decide(context)
        scan.start.assert_called_once()
        self.assertNotIn('--refresh-context', scan.start.call_args.args[0])
        self.assertFalse(context.scan_context_refresh)

    def test_refresh_respects_existing_scan_control_backoff_and_attention(self):
        for state in ('backoff', 'attention', 'busy'):
            with self.subTest(state=state):
                fixture, context, scan = self.context()
                self.request(fixture)
                if state == 'backoff':
                    context.control_retry_at['scan'] = 200
                elif state == 'attention':
                    context.control_attention['scan'] = {'reason_code': 'control_plane_failed'}
                else:
                    scan.process = object()
                self.decide(context)
                scan.start.assert_not_called()
                scan.poll.assert_called_once()

    def test_daemon_ignores_context_wakeups_outside_due_scope(self):
        for state in ('future', 'expired', 'canceled', 'out_of_scope'):
            with self.subTest(state=state):
                fixture, context, scan = self.context()
                kwargs = {}
                if state == 'future':
                    kwargs['not_before_at'] = '2099-01-01T00:00:00+00:00'
                if state == 'expired':
                    kwargs['expires_at'] = retention_fixtures.NOW
                self.request(fixture, **kwargs)
                if state == 'canceled':
                    with fixture.connect() as conn:
                        conn.execute("UPDATE wakeups SET status='canceled'")
                if state == 'out_of_scope':
                    context.repo_ids = ['unconfigured']
                self.decide(context)
                scan.start.assert_not_called()

    def test_context_scan_reports_quota_reset_without_notification_or_repo_reads(self):
        fixture = run_fixtures.RunOnceDryRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        config = validate_config.validate_config(fixture.config_path, skip_external=True)
        storage.init_database(config['db_path'])
        with closing(sqlite3.connect(config['db_path'])) as conn, conn:
            repo_id = run_once._upsert_repo(conn, config['repos'][0])
            wakeup.request_wakeup(conn, repo_id=repo_id, reason='manual_operator_request',
                dedupe_key='refresh', metadata={'kind': 'github_context_refresh'})

        def runner(args, **kwargs):
            self.assertEqual(args[:3], ['gh', 'api', 'rate_limit'])
            return subprocess.CompletedProcess(args, 0, json.dumps({'resources': {
                'core': {'remaining': 0, 'reset': 1600}, 'search': {'remaining': 30}}}), '')

        with mock.patch.object(github_scan.time, 'time', return_value=1000), \
             mock.patch.object(github_scan, '_scan_repo') as scan:
            result = github_scan.scan(fixture.config_path, runner=runner, refresh_context=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['retry_after_seconds'], 600)
        self.assertEqual(result['failures'][0]['reason'], 'core_rate_limit_floor')
        scan.assert_not_called()
        with closing(sqlite3.connect(config['db_path'])) as conn:
            self.assertEqual(conn.execute('SELECT status FROM wakeups').fetchone()[0], 'pending')

    def test_refresh_without_due_repos_makes_no_github_reads(self):
        fixture = run_fixtures.RunOnceDryRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        runner = mock.Mock(side_effect=AssertionError('unexpected remote read'))
        result = github_scan.scan(fixture.config_path, runner=runner, refresh_context=True)
        self.assertTrue(result['ok'])
        self.assertEqual(result['batch_count'], 0)
        self.assertEqual(result['github_reads'], {})
        runner.assert_not_called()
