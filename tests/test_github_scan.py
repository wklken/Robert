from tests import support  # noqa: F401
from contextlib import closing
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from robert_agent import storage


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'state.sqlite3'
        storage.init_database(self.db)

    def test_failed_route_batch_survives_and_retries_without_remote_reads(self):
        from robert_agent import github_scan
        github_scan.save_batch(self.db, 'repo', 'context', [{'id': 'one'}], [], now=100)
        batch = github_scan.next_batch(self.db, 'repo', now=100)
        self.assertEqual(batch['raw_events'], [{'id': 'one'}])
        github_scan.finish_batch(self.db, batch['batch_id'], success=False, now=100)
        self.assertIsNone(github_scan.next_batch(self.db, 'repo', now=101))
        self.assertEqual(github_scan.next_batch(self.db, 'repo', now=131)['batch_id'], batch['batch_id'])
        github_scan.finish_batch(self.db, batch['batch_id'], success=True, now=131)
        self.assertIsNone(github_scan.next_batch(self.db, 'repo', now=132))

    def test_new_batch_does_not_overwrite_unseen_events(self):
        from robert_agent import github_scan
        github_scan.save_batch(self.db, 'repo', 'context', [{'id': 'one'}], [], now=100)
        github_scan.save_batch(self.db, 'repo', 'context', [{'id': 'two'}], [], now=101)
        first = github_scan.next_batch(self.db, 'repo', now=102)
        self.assertEqual(first['raw_events'], [{'id': 'one'}])
        github_scan.finish_batch(self.db, first['batch_id'], success=True, now=102)
        self.assertEqual(github_scan.next_batch(self.db, 'repo', now=102)['raw_events'], [{'id': 'two'}])

    def test_replay_missing_remote_response_never_falls_through(self):
        from robert_agent.github_scan import ReplayRunner
        with mock.patch('subprocess.run', side_effect=AssertionError('network called')):
            with self.assertRaises(subprocess.CalledProcessError):
                ReplayRunner([])(['gh', 'api', 'repos/example/repo/issues/1'], check=True)

    def test_open_pr_reconcile_does_not_fetch_pull_details(self):
        from robert_agent import run_once
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, json.dumps({'state': 'open'}), '')
        result = run_once._remote_source_state('example/repo', 'pull_request', 1, runner)
        self.assertEqual(result['state'], 'open')
        self.assertEqual(len(calls), 1)

    def test_old_batch_uses_latest_remote_state(self):
        from robert_agent import github_scan
        key = github_scan.command_key(['gh', 'api', 'repos/example/repo/issues/1'])
        github_scan.save_batch(self.db, 'repo', 'context', [{'id': 'one'}], {key: '{"state":"open"}'}, now=100)
        github_scan.save_batch(self.db, 'repo', 'context', [], {key: '{"state":"closed"}'}, now=101)
        batch = github_scan.next_batch(self.db, 'repo', now=102)
        response = github_scan.ReplayRunner(batch['responses'])(['gh', 'api', 'repos/example/repo/issues/1'])
        self.assertEqual(json.loads(response.stdout)['state'], 'closed')

    def test_owned_background_process_does_not_hold_local_database(self):
        import sys
        import time
        from robert_agent.control_lane import ControlLane
        from robert_agent import github_scan
        lane = ControlLane(self.db, 'scan', 'test-owner')
        self.assertTrue(lane.start([sys.executable, '-c', 'import time; time.sleep(30)'], timeout_seconds=40))
        self.addCleanup(lane.stop)
        self.assertIsNone(lane.poll())
        github_scan.save_batch(self.db, 'repo', 'context', [{'id': 'ready'}], [], now=100)
        started = time.monotonic()
        batch = github_scan.next_batch(self.db, 'repo', now=100)
        github_scan.finish_batch(self.db, batch['batch_id'], success=True, now=100)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNone(lane.poll())
        self.assertTrue(lane.stop())

    def test_new_read_failure_invalidates_old_success(self):
        from robert_agent import github_scan
        key = github_scan.command_key(['gh', 'api', 'repos/example/repo/issues/1'])
        github_scan.save_batch(self.db, 'repo', 'context', [{'id': 'one'}], {key: '{"state":"open"}'}, now=100)
        github_scan.save_batch(self.db, 'repo', 'context', [], {key: None}, now=101)
        batch = github_scan.next_batch(self.db, 'repo', now=102)
        with self.assertRaises(subprocess.CalledProcessError):
            github_scan.ReplayRunner(batch['responses'])(['gh', 'api', 'repos/example/repo/issues/1'], check=True)

    def test_same_pending_batch_coalesces_and_context_change_waits_for_refresh(self):
        from robert_agent import github_scan
        event = {'id': 'one', 'number': 7, 'source_type': 'issue'}
        first = github_scan.save_batch(self.db, 'repo', 'old', [event], {}, now=100)
        self.assertEqual(github_scan.save_batch(self.db, 'repo', 'old', [event], {}, now=101), first)
        self.assertIsNone(github_scan.next_batch(self.db, 'repo', context='new', now=102))
        fresh = github_scan.save_batch(self.db, 'repo', 'new', [], {}, now=103, complete_sources=[('issue', 7)])
        github_scan.finish_batch(self.db, fresh, success=True, now=104)
        self.assertEqual(github_scan.next_batch(self.db, 'repo', now=104)['raw_events'], [event])

    def test_spawn_failure_closes_handles_and_cancels_starting_row(self):
        from robert_agent.control_lane import ControlLane
        import sqlite3
        lane = ControlLane(self.db, 'scan', 'owner')
        with mock.patch('subprocess.Popen', side_effect=OSError('spawn failed')):
            with self.assertRaises(OSError):
                lane.start(['invalid'], timeout_seconds=10)
        self.assertIsNone(lane.output)
        self.assertIsNone(lane.error)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute('SELECT status FROM github_control_children').fetchone()[0], 'stopped')

    def test_recovery_ignores_other_scope_and_live_owner(self):
        from robert_agent import control_lane, daemon_state
        import sqlite3
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for owner, scope, when, ttl in [('other', 'other-scope', '2020-01-01T00:00:00+00:00', 1),
                                           ('healthy', 'current-scope', '2099-01-01T00:00:00+00:00', 99999)]:
                daemon_state.start_daemon_run(conn, config_path='/config', owner_id=owner, now=when)
                daemon_state.acquire_daemon_lease(conn, resource_key=scope, owner_id=owner, ttl_seconds=ttl, now=when)
                conn.execute("INSERT INTO github_control_children VALUES (?,?,?,'{}','running')", (owner, owner, 'scan'))
        with mock.patch.object(control_lane.worker_identity, 'terminate_recorded_identity', side_effect=AssertionError('must not stop healthy/unrelated child')):
            self.assertTrue(control_lane.recover(self.db, scope_key='current-scope', current_owner='new'))

    def test_real_local_routing_and_finalization_while_scanner_is_running(self):
        import sqlite3
        import sys
        from tests.test_run_once import RunOnceDryRunTests
        from robert_agent import github_scan, run_once, validate_config
        from robert_agent.control_lane import ControlLane
        from robert_agent.worker import result as worker_result
        fixture = RunOnceDryRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        config = validate_config.validate_config(fixture.config_path, skip_external=True)
        db = config['db_path']
        storage.init_database(db)
        with closing(sqlite3.connect(db)) as conn, conn:
            repo_id = run_once._upsert_repo(conn, config['repos'][0])
        lane = ControlLane(db, 'scan', 'test-owner')
        lane.start([sys.executable, '-c', 'import time; time.sleep(30)'], timeout_seconds=40)
        self.addCleanup(lane.stop)
        publisher_lane = ControlLane(db, 'publish', 'test-owner')
        publisher_lane.start([sys.executable, '-c', 'import time; time.sleep(30)'], timeout_seconds=40)
        self.addCleanup(publisher_lane.stop)
        event = {'id': 'analysis-one', 'number': 123, 'source_type': 'issue', 'event_type': 'comment',
                 'actor_login': 'wklken', 'body': '@robert-bot please analyze this', 'intent': 'analysis',
                 'state': 'open', 'url': 'https://github.com/example/backend/issues/123'}
        reads = {github_scan.command_key(['gh', 'api', 'repos/example/backend/issues/123']): '{"state":"open"}'}
        github_scan.save_batch(db, repo_id, github_scan.context_digest(config['repos'][0], []), [event], reads)
        launched = []
        def launch(**kwargs):
            launched.append(kwargs['task_id'])
            return {'ok': True, 'status': 'running', 'task_id': kwargs['task_id'],
                    'attempt_id': kwargs['attempt_id'], 'pid': None, 'command': ['mock']}
        with mock.patch.object(run_once.dispatch, 'dispatch_worker', side_effect=launch):
            first = run_once.run_once(fixture.config_path, dry_run=False, skip_external=True,
                                      skip_publish=True, consume_observations=True)
        self.assertTrue(first['ok'], first)
        self.assertEqual(len(launched), 1)
        self.assertIsNone(lane.poll())
        with closing(sqlite3.connect(db)) as conn, conn:
            task, attempt = conn.execute('SELECT task_id,attempt_id FROM attempts').fetchone()
        payload = {'task_id': task, 'attempt_id': attempt, 'output_type': 'comment_analysis',
                   'planned_github_actions': [{'type': 'comment', 'target_url': event['url'],
                       'body': f'<!-- robert-comment task_id={task} attempt_id={attempt} event_fingerprints=comment:analysis-one -->\\nReady'}],
                   'consumed_event_fingerprints': ['comment:analysis-one'], 'verification': [],
                   'used_skills': [], 'handoff': 'ready'}
        self.assertTrue(worker_result.record_result(db, payload)['ok'])
        run_once.run_once(fixture.config_path, dry_run=False, skip_external=True,
                          skip_publish=True, consume_observations=True)
        with closing(sqlite3.connect(db)) as conn, conn:
            self.assertEqual(conn.execute('SELECT audit_status FROM github_actions').fetchone()[0], 'accepted')
            # Publisher completion is already recorded; local completion must
            # proceed without invoking publication from this drain.
            conn.execute("UPDATE github_actions SET publish_status='published'")
            from robert_agent import loop_engine
            self.assertTrue(loop_engine.has_runnable_local_work(conn, repo_id=repo_id))
        second = run_once.run_once(fixture.config_path, dry_run=False, skip_external=True,
                                   skip_publish=True, consume_observations=True)
        self.assertTrue(second['ok'], second)
        with closing(sqlite3.connect(db)) as conn, conn:
            self.assertEqual(conn.execute('SELECT lifecycle FROM tasks').fetchone()[0], 'completed')
        self.assertIsNone(lane.poll())
        self.assertTrue(lane.stop())
        self.assertIsNone(publisher_lane.poll())
        self.assertTrue(publisher_lane.stop())

    def test_effective_global_review_config_changes_context(self):
        from robert_agent import github_scan, route_config
        repo = {'full_name': 'example/backend', 'github_account': 'bot'}
        with mock.patch.object(route_config, 'resolve_route_config', return_value={
                'multi_agent_review': {'enabled': True, 'workers': ['a', 'b'], 'author_allowlist': ['alice']}}):
            first = github_scan.context_digest(repo, [], config={})
        with mock.patch.object(route_config, 'resolve_route_config', return_value={
                'multi_agent_review': {'enabled': True, 'workers': ['a', 'b'], 'author_allowlist': ['bob']}}):
            self.assertNotEqual(first, github_scan.context_digest(repo, [], config={}))

    def test_refresh_retains_unconsumed_identity_and_rechecks_trigger(self):
        from robert_agent import github_scan
        repo = {'full_name': 'example/backend', 'github_account': 'bot', 'trusted_actors': ['new-user']}
        old = {'id': 'old', 'event_type': 'notification', 'number': 7, 'source_type': 'issue',
               'actor_login': 'removed-user', 'trusted_trigger_found': True, 'body': '@bot old request'}
        new = {**old, 'id': 'new', 'actor_login': 'new-user'}
        events = github_scan.refresh_pending_events([new], [old], {('issue', 7)}, repo)
        self.assertEqual({event['id'] for event in events}, {'old', 'new'})
        self.assertFalse(next(event for event in events if event['id'] == 'old')['trusted_trigger_found'])

    def test_ready_repo_dispatches_before_next_local_repo_pipeline(self):
        import yaml
        from tests.test_run_once import RunOnceDryRunTests
        from robert_agent import run_once
        fixture = RunOnceDryRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        config = yaml.safe_load(fixture.config_path.read_text())
        second = {**config['repos'][0], 'full_name': 'example/second'}
        config['repos'].append(second)
        fixture.config_path.write_text(yaml.safe_dump(config))
        original = run_once._run_repo_pipeline
        order = []
        def pipeline(*args, **kwargs):
            order.append(kwargs['repo']['full_name'])
            result = original(*args, **kwargs)
            if len(order) == 1:
                kwargs['dispatch_queue'].append({'sentinel': True})
            return result
        def dispatch(*args, **kwargs):
            if args[2]:
                order.append('dispatched')
            return []
        with mock.patch.object(run_once, '_run_repo_pipeline', side_effect=pipeline), \
             mock.patch.object(run_once, '_dispatch_queued', side_effect=dispatch):
            result = run_once.run_once(fixture.config_path, skip_external=True, skip_publish=True,
                                       consume_observations=True, dry_run=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(order, ['example/backend', 'dispatched', 'example/second'])

    def test_old_remote_safety_read_expires_even_while_batch_is_pending(self):
        from robert_agent import github_scan
        key = github_scan.command_key(['gh', 'api', 'repos/example/repo/issues/1'])
        github_scan.save_batch(self.db, 'repo', 'context', [{'id': 'one'}], {key: '{"state":"open"}'}, now=100)
        batch = github_scan.next_batch(self.db, 'repo', now=4000)
        with self.assertRaises(subprocess.CalledProcessError):
            github_scan.ReplayRunner(batch['responses'])(['gh', 'api', 'repos/example/repo/issues/1'], check=True)

    def test_core_quota_floor_uses_reset_before_retrying(self):
        from robert_agent import github_scan
        session = mock.Mock()
        session.poll_interval_seconds = None
        config = {'daemon': {'min_core_remaining': 500, 'min_search_remaining': 10}}
        with mock.patch.object(github_scan.discover, '_run_json', return_value={
                'resources': {'core': {'remaining': 3, 'reset': 1500}, 'search': {'remaining': 30}}}), \
             mock.patch.object(github_scan.time, 'time', return_value=1000):
            self.assertFalse(github_scan._budget(session, config))
        self.assertEqual(session.poll_interval_seconds, 500)

    def test_background_decision_drains_while_remote_lanes_are_busy(self):
        from types import SimpleNamespace
        from tests.test_retention import RetentionTests, NOW
        from robert_agent import daemon
        fixture = RetentionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        context = fixture.context
        scan = SimpleNamespace(process=object(), poll=mock.Mock(return_value=None), start=mock.Mock())
        publish = SimpleNamespace(process=object(), poll=mock.Mock(return_value=None), start=mock.Mock())
        context.background_lanes = {'scan': scan, 'publish': publish}
        context.startup_live_poll_pending = True
        with mock.patch.dict('os.environ', {'ROBERT_NOTIFICATION_COMMAND': 'configured-but-not-run'}), \
             mock.patch.object(daemon, '_maybe_prune'), \
             mock.patch.object(daemon.loop_engine, 'has_runnable_local_work', return_value=True), \
             mock.patch.object(daemon, '_run_local_drain', return_value={'ok': True}) as drain:
            result = daemon.run_once_decision(context, now=NOW)
        self.assertEqual(result['decision'], 'local_drain')
        self.assertTrue(drain.call_args.kwargs['skip_publish'])
        scan.start.assert_not_called()
        publish.start.assert_not_called()

    def test_context_refresh_does_not_advance_the_notification_poll_interval(self):
        from types import SimpleNamespace
        from tests.test_retention import RetentionTests, NOW
        from robert_agent import daemon, wakeup
        fixture = RetentionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        context = fixture.context
        context.startup_live_poll_pending = False
        scan = SimpleNamespace(process=None, poll=mock.Mock(return_value={
            'ok': True, 'poll_interval_seconds': 90}), start=mock.Mock())
        publish = SimpleNamespace(process=None, poll=mock.Mock(return_value=None), start=mock.Mock())
        context.background_lanes = {'scan': scan, 'publish': publish}
        with fixture.connect() as conn:
            wakeup.request_wakeup(conn, repo_id='repo', reason='manual_operator_request',
                                  dedupe_key='refresh', metadata={'kind': 'github_context_refresh'}, now=NOW)
        with mock.patch.object(daemon, '_maybe_prune'), \
             mock.patch.object(daemon.loop_engine, 'has_runnable_local_work', return_value=False):
            daemon.run_once_decision(context, now=NOW)
        self.assertEqual(context.next_live_poll_at, '2026-10-23T00:01:30+00:00')
        self.assertIn('--refresh-context', scan.start.call_args.args[0])

    def test_retention_keeps_lease_until_owned_control_children_stop(self):
        from tests.test_retention import RetentionTests, NOW, OLD
        from robert_agent import retention
        fixture = RetentionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with fixture.connect() as conn:
            conn.execute("UPDATE daemon_runs SET status='stopped'")
            conn.execute("UPDATE leases SET status='released',released_at=?", (OLD,))
            conn.execute("INSERT INTO github_control_children VALUES ('child','daemon','scan','{}','running')")
        result = retention.prune_database(fixture.db, apply=True, now=NOW)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['counts']['leases'], 0)
        with fixture.connect() as conn:
            conn.execute("UPDATE github_control_children SET status='stopped'")
        result = retention.prune_database(fixture.db, apply=True, now=NOW)
        self.assertEqual(result['counts']['leases'], 1)

    def test_scan_failure_preserves_new_safety_read_over_old_success(self):
        from tests.test_run_once import RunOnceDryRunTests
        from robert_agent import github_scan, run_once, validate_config
        fixture = RunOnceDryRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        config = validate_config.validate_config(fixture.config_path, skip_external=True)
        db = config['db_path']
        storage.init_database(db)
        import sqlite3
        with closing(sqlite3.connect(db)) as conn, conn:
            repo_id = run_once._upsert_repo(conn, config['repos'][0])
        event = {'id': 'old', 'number': 7, 'source_type': 'issue', 'event_type': 'comment'}
        key = github_scan.command_key(['gh', 'api', 'repos/example/backend/issues/7'])
        github_scan.save_batch(db, repo_id, 'context', [event], {key: '{"state":"open"}'})
        session = mock.Mock(side_effect=lambda args, **kwargs: subprocess.CompletedProcess(args, 0, '{"state":"closed"}', ''))
        session.metrics.return_value = {'errors': 0}
        session.collect_events.side_effect = subprocess.CalledProcessError(1, ['gh', 'search'])
        with self.assertRaises(subprocess.CalledProcessError):
            github_scan._scan_repo(config, config['repos'][0], session, [])
        batch = github_scan.next_batch(db, repo_id)
        self.assertEqual(json.loads(batch['responses'][key])['state'], 'closed')

    def test_context_refresh_uses_cached_hints_without_repolling_notifications(self):
        from tests import test_run_once as fixtures
        from robert_agent import github_scan
        fixture = fixtures.RunOnceDryRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        session = mock.Mock()
        session.poll_interval_seconds = None
        session.metrics.return_value = {}
        session.cached_notifications.return_value = {'example/backend': [{'id': 'hint'}]}
        with mock.patch.object(github_scan, 'GitHubReadSession', return_value=session), \
             mock.patch.object(github_scan, '_budget', return_value=True), \
             mock.patch.object(github_scan, '_scan_repo', return_value='batch') as scan:
            result = github_scan.scan(fixture.config_path, refresh_context=True)
        self.assertTrue(result['ok'])
        self.assertTrue(result['context_refresh'])
        self.assertEqual(scan.call_args.args[-1], [{'id': 'hint'}])
        session.collect_notifications.assert_not_called()

    def test_deadlines_escape_budget_notifications_repository_and_publisher(self):
        from tests import test_run_once as fixtures
        from robert_agent import github_scan
        from robert_agent.common import ControlDeadlineExceeded
        fixture = fixtures.RunOnceDryRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        failure = ControlDeadlineExceeded('read', 0)
        for phase in ('budget', 'notifications', 'repo', 'publish'):
            with self.subTest(phase=phase):
                session = mock.Mock()
                session.metrics.return_value = {}
                session.collect_notifications.return_value = {}
                if phase == 'notifications':
                    session.collect_notifications.side_effect = failure
                with mock.patch.object(github_scan, 'GitHubReadSession', return_value=session), \
                     mock.patch.object(github_scan, '_budget', side_effect=failure if phase in ('budget', 'publish') else None, return_value=True), \
                     mock.patch.object(github_scan, '_scan_repo', side_effect=failure if phase == 'repo' else None), \
                     self.assertRaises(ControlDeadlineExceeded):
                    if phase == 'publish':
                        github_scan.publish(fixture.config_path)
                    else:
                        github_scan.scan(fixture.config_path)
