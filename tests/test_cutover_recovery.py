"""Incompatible task results require attention without stopping other work."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import sys
import time
import unittest
from unittest.mock import patch

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import board, daemon, daemon_state, result_cutover, run_once, storage


class CutoverRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'db.sqlite'
        storage.init_database(self.db)
        self.now = '2026-10-31T00:00:00+00:00'
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES('repo','x/y','bot','main','/repo','/trees')")
            for name in ('bad', 'good'):
                conn.execute("INSERT INTO workstreams(workstream_id,repo_id,lifecycle,active_task_id,created_at,updated_at) VALUES(?,'repo','active',?,?,?)", (name, name, self.now, self.now))
                conn.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES(?,?,'queued',?,?)", (name, name, self.now, self.now))
                conn.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status,started_at,heartbeat_at,metadata_json) VALUES(?,?,1,'running',?,?,?)", (name, name, self.now, self.now, json.dumps({'result_contract': 'compact-v1', 'dispatch': {'pid': 123456789}})))
            conn.execute("INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at) VALUES('old','bad','bad','classification_result',?)", (self.now,))
            conn.execute("INSERT INTO work_items(work_item_id,repo_id,title,origin_type,workstream_id,creation_idempotency_key,created_by,created_at,updated_at) VALUES('item','repo','Bad request','web','bad','item','operator',?,?)", (self.now, self.now))

    def test_quarantine_preserves_result_and_shows_waiting_without_blocking_good_task(self):
        with patch.object(run_once, '_dispatch_process_status', return_value={'status': 'dead'}):
            first = result_cutover.prepare(self.db, now=self.now)
            second = result_cutover.prepare(self.db, now=self.now)
        self.assertTrue(first['ok'], first)
        self.assertTrue(second['ok'], second)
        self.assertTrue(result_cutover.check(self.db)['ok'])
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            self.assertEqual(conn.execute("SELECT output_type,metadata_json FROM worker_results WHERE result_id='old'").fetchone()[:], ('classification_result', '{}'))
            self.assertEqual(conn.execute("SELECT status FROM attempts WHERE task_id='bad'").fetchone()[0], 'failed')
            self.assertEqual(conn.execute("SELECT status FROM attempts WHERE task_id='good'").fetchone()[0], 'running')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM work_item_events').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks t WHERE t.task_id='good' AND " + run_once.execution_store.runnable_sql()).fetchone()[0], 1)
        card = board.get_work_item_detail(self.db, 'item')
        self.assertEqual(card['column'], 'waiting', card)

    def test_live_or_unknown_worker_keeps_capacity_and_is_not_signaled(self):
        for status in ('alive_same', 'unknown'):
            with self.subTest(status=status), patch.object(run_once, '_dispatch_process_status', return_value={'status': status}), patch.object(run_once, '_terminate_attempt_process') as terminate:
                prepared = result_cutover.prepare(self.db, now=self.now)
                self.assertTrue(prepared['ok'], prepared)
                terminate.assert_not_called()
                with closing(sqlite3.connect(self.db)) as conn:
                    self.assertEqual(conn.execute("SELECT status FROM attempts WHERE task_id='bad'").fetchone()[0], 'running')

    def test_unidentifiable_spool_remains_global_and_preserved(self):
        spool = Path(str(self.db) + '.results')
        spool.mkdir()
        record = spool / 'legacy.json'
        record.write_text('{"payload": {}}')
        with patch.object(run_once, '_dispatch_process_status', return_value={'status': 'dead'}):
            checked = result_cutover.prepare(self.db, now=self.now)
        self.assertFalse(checked['ok'], checked)
        self.assertEqual(record.read_text(), '{"payload": {}}')

    def context(self):
        with closing(daemon_state.connect(self.db)) as conn, conn:
            run = daemon_state.start_daemon_run(conn, config_path='config', owner_id='owner', now=self.now)
        return daemon.DaemonContext('config', None, self.db, 'repo', 3,
            {'local_poll_seconds': 5, 'github_poll_seconds': 300, 'local_drain_timeout_seconds': 180, 'live_run_timeout_seconds': 180},
            run['daemon_run_id'], 'lease', repo_ids=['repo'])

    def test_control_failure_stops_after_three_attempts_and_deduplicates_attention(self):
        context = self.context()
        failure = {'ok': False, 'status': 'child_failed', 'safe_error': 'database unavailable'}
        with patch.object(daemon, '_reconcile_child_result'), patch.object(daemon.time, 'monotonic', return_value=100):
            for _ in range(3):
                context.control_retry_at['local'] = 0
                daemon._run_local_drain(context, child_runner=lambda *a, **k: failure, now=self.now)
        self.assertIn('local', context.control_attention)
        with patch.object(daemon, '_reconcile_child_result'), patch.object(daemon.time, 'monotonic', return_value=100):
            result = daemon._run_local_drain(context, child_runner=lambda *a, **k: self.fail('must not launch a fourth child'), now=self.now)
        self.assertEqual(result['status'], 'control_plane_attention')
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications WHERE notification_type='control_plane_failed'").fetchone()[0], 1)
        self.assertGreaterEqual(daemon._sleep_seconds_after_decision(context, {'decision': 'control_attention'}), 5)

    def test_success_resets_failure_budget(self):
        context = self.context()
        with patch.object(daemon, '_reconcile_child_result'), patch.object(daemon.time, 'monotonic', return_value=100):
            daemon._run_local_drain(context, child_runner=lambda *a, **k: {'ok': False}, now=self.now)
            context.control_retry_at['local'] = 0
            daemon._run_local_drain(context, child_runner=lambda *a, **k: {'ok': True}, now=self.now)
        self.assertEqual(context.control_failures.get('local', 0), 0)

    def test_more_than_fifty_blockers_are_isolated_in_one_preflight(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for index in range(65):
                task = 'legacy-' + str(index)
                conn.execute("INSERT INTO workstreams(workstream_id,repo_id,lifecycle,created_at,updated_at) VALUES(?,'repo','active',?,?)", (task, self.now, self.now))
                conn.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES(?,?,'queued',?,?)", (task, task, self.now, self.now))
                conn.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json) VALUES(?,?,1,'running',?)", (task, task, json.dumps({'dispatch': {'pid': 123456789}})))
        with patch.object(run_once, '_dispatch_process_status', return_value={'status': 'dead'}):
            self.assertTrue(result_cutover.prepare(self.db, now=self.now)['ok'])

    def test_local_backoff_does_not_pause_scan_lane(self):
        context = self.context()
        with patch.object(daemon, '_reconcile_child_result'), patch.object(daemon.time, 'monotonic', return_value=100):
            daemon._run_local_drain(context, child_runner=lambda *a, **k: {'ok': False}, now=self.now)
            result = daemon._run_local_drain(context, child_runner=lambda *a, **k: self.fail('backoff bypassed'), now=self.now)
            self.assertEqual(result['status'], 'control_backoff')
            self.assertTrue(daemon._run_live_poll(context, child_runner=lambda *a, **k: {'ok': True}, now=self.now)['ok'])

    def test_explicit_retrigger_preserves_old_evidence_and_refreshes_source_body(self):
        from robert_agent import discover, loop_engine, work_items
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number,html_url,state) VALUES('source','repo','github:x/y#205','issue',205,'https://github.com/x/y/issues/205','open')")
            conn.execute("UPDATE workstreams SET primary_source_id='source' WHERE workstream_id='bad'")
            event = {'event_type': 'assigned', 'number': 205, 'body': '', 'event_fingerprint': 'assignment:205'}
            conn.execute("INSERT INTO github_events(event_id,repo_id,source_id,event_type,event_fingerprint,payload_json,event_at) VALUES('event','repo','source','assigned','assignment:205',?,?)", (json.dumps(event), self.now))
            conn.execute("INSERT INTO task_events(task_id,event_id,relationship,created_at) VALUES('bad','event','trigger',?)", (self.now,))
        with patch.object(run_once, '_dispatch_process_status', return_value={'status': 'dead'}):
            result_cutover.prepare(self.db, now=self.now)
            with patch.object(discover, '_source_metadata', return_value=({**event, 'body': 'suppress specified plugin warning'}, 'open')):
                result = work_items.retrigger_failed_github_task(self.db, 'bad',
                    context=work_items.CommandContext('operator', 'owner', frozenset({'repo'}), frozenset()),
                    idempotency_key='recover', notifier=lambda _: None)
        self.assertTrue(result['ok'])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertTrue(loop_engine.has_runnable_local_work(conn, repo_ids=['repo']))
            metadata = json.loads(conn.execute("SELECT metadata_json FROM wakeups WHERE wakeup_id=?", (result['wakeup_id'],)).fetchone()[0])
            self.assertEqual(metadata['refreshed_context'][0]['body'], 'suppress specified plugin warning')
            self.assertEqual(json.loads(conn.execute("SELECT payload_json FROM github_events WHERE event_id='event'").fetchone()[0])['body'], '')
            self.assertIsNotNone(conn.execute("SELECT 1 FROM worker_results WHERE result_id='old'").fetchone())

    def test_refreshed_context_reaches_followup_without_changing_trigger_authority(self):
        event = {'body': '', 'event_fingerprint': 'assignment:205', 'authorization_status': 'authorized_trigger'}
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number,html_url,state) VALUES('source','repo','github:x/y#205','issue',205,'https://github.com/x/y/issues/205','open')")
            conn.execute("INSERT INTO github_events(event_id,repo_id,source_id,event_type,event_fingerprint,payload_json,event_at) VALUES('event','repo','source','assigned','assignment:205',?,?)", (json.dumps(event), self.now))
            conn.execute("INSERT INTO task_events(task_id,event_id,relationship,created_at) VALUES('good','event','trigger',?)", (self.now,))
            conn.execute("UPDATE tasks SET metadata_json=? WHERE task_id='good'", (json.dumps({'refreshed_context': [{**event, 'body': 'actual issue body', 'authorization_status': 'ignored'}]}),))
            context = run_once._task_related_events(conn, 'good', include_consumed=True)
        self.assertEqual(context[0]['body'], 'actual issue body')
        self.assertEqual(context[0]['authorization_status'], 'authorized_trigger')

    def test_cutover_retrigger_rejects_live_worker_and_pending_publication(self):
        from robert_agent import discover, work_items
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number,html_url,state) VALUES('source','repo','github:x/y#205','issue',205,'https://github.com/x/y/issues/205','open')")
            conn.execute("UPDATE workstreams SET primary_source_id='source' WHERE workstream_id='bad'")
            event = {'event_type': 'notification', 'number': 205, 'event_fingerprint': 'assignment:205'}
            conn.execute("INSERT INTO github_events(event_id,repo_id,source_id,event_type,event_fingerprint,payload_json,event_at) VALUES('event','repo','source','notification','assignment:205',?,?)", (json.dumps(event), self.now))
            conn.execute("INSERT INTO task_events(task_id,event_id,relationship,created_at) VALUES('bad','event','trigger',?)", (self.now,))
        context = work_items.CommandContext('operator', 'owner', frozenset({'repo'}), frozenset())
        with patch.object(run_once, '_dispatch_process_status', return_value={'status': 'alive_same'}):
            result_cutover.prepare(self.db, now=self.now)
        with patch.object(discover, '_source_metadata', return_value=(event, 'open')):
            with self.assertRaisesRegex(work_items.WorkItemConflictError, 'termination'):
                work_items.retrigger_failed_github_task(self.db, 'bad', context=context, idempotency_key='live')
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE attempts SET status='failed' WHERE task_id='bad'")
                conn.execute("INSERT INTO github_actions(action_id,result_id,task_id,action_type,audit_status,publish_status,created_at) VALUES('pending','old','bad','comment','pending','not_published',?)", (self.now,))
            with self.assertRaisesRegex(work_items.WorkItemConflictError, 'publication'):
                work_items.retrigger_failed_github_task(self.db, 'bad', context=context, idempotency_key='publication')
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM wakeups').fetchone()[0], 0)
            self.assertIsNotNone(json.loads(conn.execute("SELECT metadata_json FROM tasks WHERE task_id='bad'").fetchone()[0]).get('execution_attention'))

    def test_unknown_spool_stops_control_immediately_and_stays_latched(self):
        context = self.context()
        failure = {'ok': False, 'status': 'blocked_result_contract_cutover', 'required_action': 'Inspect spool.'}
        with patch.object(daemon.time, 'monotonic', return_value=100):
            daemon._record_control_outcome(context, 'local', failure, now=self.now)
        self.assertEqual(context.control_failures['local'], 1)
        with patch.object(result_cutover, 'check', side_effect=AssertionError('must not repeat blocked guard')), patch.object(daemon, '_maybe_prune'), patch.object(daemon.notify, 'deliver_pending'):
            self.assertEqual(daemon.run_once_decision(context, now=self.now)['decision'], 'control_attention')

    def test_loop_error_diagnostics_keep_the_contract_blocker(self):
        payload = daemon._event_result({'ok': False, 'status': 'failed', 'cycle_results': [{
            'ok': False, 'status': 'blocked_result_contract_cutover',
            'blockers': [{'kind': 'legacy_unresolved_result', 'task_id': 'bad', 'result_id': 'old'}],
            'required_action': 'Inspect incompatible task results.'}]})
        self.assertEqual(payload['cause_status'], 'blocked_result_contract_cutover')
        self.assertEqual(payload['blockers'][0]['task_id'], 'bad')

    def github_item(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number,html_url,state) VALUES('source','repo','github:x/y#205','issue',205,'https://github.com/x/y/issues/205','open')")
            conn.execute("UPDATE workstreams SET primary_source_id='source' WHERE workstream_id='bad'")
            conn.execute("UPDATE work_items SET origin_type='github',origin_source_id='source' WHERE work_item_id='item'")
            conn.execute("INSERT INTO github_events(event_id,repo_id,source_id,event_type,event_fingerprint,payload_json,event_at) VALUES('event','repo','source','assigned','assignment:205',?,?)", (json.dumps({'event_type': 'assigned', 'number': 205}), self.now))
            conn.execute("INSERT INTO task_events(task_id,event_id,relationship,created_at) VALUES('bad','event','trigger',?)", (self.now,))

    def test_isolated_worker_exit_releases_capacity_and_allows_retrigger(self):
        from robert_agent import discover, supervise, work_items
        self.github_item()
        with patch.object(run_once, '_dispatch_process_status', return_value={'status': 'alive_same'}):
            result_cutover.prepare(self.db, now=self.now)
        with patch.object(run_once, '_dispatch_process_status', return_value={'status': 'dead'}):
            self.assertTrue(result_cutover.prepare(self.db, now=self.now)['ok'])
            with closing(sqlite3.connect(self.db)) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM attempts a WHERE task_id=\'bad\' AND ' + supervise.occupied_attempt_sql()).fetchone()[0], 0)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM work_item_events').fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT output_type FROM worker_results WHERE result_id='old'").fetchone()[0], 'classification_result')
            context = work_items.CommandContext('operator', 'owner', frozenset({'repo'}), frozenset())
            with patch.object(discover, '_source_metadata', return_value=({'body': 'refreshed'}, 'open')):
                requested = work_items.retrigger_failed_github_task(self.db, 'bad', context=context, idempotency_key='after-exit')
            self.assertTrue(requested['ok'], requested)

    def test_isolated_completed_worker_exit_releases_capacity_without_rewriting_result(self):
        from robert_agent import supervise
        with patch.object(run_once, '_dispatch_process_status', return_value={'status': 'alive_same'}):
            result_cutover.prepare(self.db, now=self.now)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempts SET status='completed',finished_at='original' WHERE task_id='bad'")
        with patch.object(run_once, '_dispatch_process_status', return_value={'status': 'dead'}):
            result_cutover.prepare(self.db, now=self.now)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM attempts a WHERE task_id=\'bad\' AND ' + supervise.occupied_attempt_sql()).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT status,finished_at FROM attempts WHERE task_id='bad'").fetchone(), ('completed', 'original'))

    def test_global_cutover_pause_still_times_out_real_control_children(self):
        from robert_agent.control_lane import ControlLane
        context = self.context()
        for latched in (False, True):
            with self.subTest(latched=latched):
                context.control_attention.clear()
                lane = ControlLane(self.db, 'scan', context.daemon_run_id)
                self.addCleanup(lane.stop)
                context.background_lanes = {'scan': lane}
                lane.start([sys.executable, '-c', 'import time; time.sleep(30)'], timeout_seconds=10)
                process = lane.process
                deadline = time.monotonic() + 5
                while lane._state() == 'starting' and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(lane._state(), 'running')
                lane.started = time.monotonic() - 20
                failure = {'ok': False, 'status': 'blocked_result_contract_cutover'}
                if latched:
                    daemon._record_control_outcome(context, 'local', failure, now=self.now)
                with patch.object(result_cutover, 'prepare', return_value=failure), patch.object(daemon, '_maybe_prune'), patch.object(daemon.notify, 'deliver_pending'), patch.object(lane, 'start', side_effect=AssertionError('paused lane must not start')):
                    decision = daemon.run_once_decision(context, now=self.now)
                self.assertEqual(decision['decision'], 'control_attention')
                self.assertIsNotNone(process.poll(), 'existing child exceeded its deadline while globally paused')
                self.assertIsNone(lane.process)
                with closing(sqlite3.connect(self.db)) as conn:
                    self.assertEqual(conn.execute('SELECT status FROM github_control_children WHERE child_id=?', (lane.identifier,)).fetchone()[0], 'stopped')
                    event = conn.execute("SELECT metadata_json FROM daemon_events WHERE daemon_run_id=? AND event_type='scan_completed'", (context.daemon_run_id,)).fetchone()
                    self.assertEqual(json.loads(event[0])['status'], 'child_timeout')

    def test_latched_pause_reaps_isolated_attempt_without_resuming_guard(self):
        with patch.object(run_once, '_dispatch_process_status', return_value={'status': 'alive_same'}):
            result_cutover.prepare(self.db, now=self.now)
        context = self.context()
        daemon._record_control_outcome(context, 'local', {'ok': False, 'status': 'blocked_result_contract_cutover'}, now=self.now)
        with patch.object(run_once, '_dispatch_process_status', return_value={'status': 'dead'}), patch.object(result_cutover, 'check', side_effect=AssertionError('latched guard must not resume')), patch.object(daemon, '_maybe_prune'), patch.object(daemon.notify, 'deliver_pending'):
            daemon.run_once_decision(context, now=self.now)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM attempts WHERE task_id='bad'").fetchone()[0], 'failed')
        self.assertIn('local', context.control_attention)

    def test_main_board_offers_retrigger_only_after_isolated_worker_stops(self):
        self.github_item()
        with patch.object(run_once, '_dispatch_process_status', return_value={'status': 'alive_same'}):
            result_cutover.prepare(self.db, now=self.now)
        self.assertEqual(board.get_work_item_detail(self.db, 'item')['valid_commands'], ['cancel'])
        with patch.object(run_once, '_dispatch_process_status', return_value={'status': 'dead'}):
            result_cutover.prepare(self.db, now=self.now)
        self.assertEqual(board.get_work_item_detail(self.db, 'item')['valid_commands'], ['retrigger', 'cancel'])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO github_actions(action_id,result_id,task_id,action_type,audit_status,publish_status,created_at) VALUES('pending','old','bad','comment','pending','not_published',?)", (self.now,))
        self.assertEqual(board.get_work_item_detail(self.db, 'item')['valid_commands'], ['cancel'])

    def test_ordinary_approve_cannot_resolve_cutover_isolation(self):
        from robert_agent import work_items
        self.github_item()
        with patch.object(run_once, '_dispatch_process_status', return_value={'status': 'dead'}):
            result_cutover.prepare(self.db, now=self.now)
        card = board.get_work_item_detail(self.db, 'item')
        context = work_items.CommandContext('operator', 'owner', frozenset({'repo'}), frozenset())
        with self.assertRaisesRegex(work_items.WorkItemConflictError, 'Retrigger'):
            work_items.execute_command(self.db, 'item', command='approve', context=context, expected_version=card['version'], idempotency_key='approve-cutover')
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM work_item_events').fetchone()[0], 1)

    def test_isolated_exit_observation_retains_unknown_identity_and_native_ownership(self):
        from robert_agent import native_rounds
        with patch.object(run_once, '_dispatch_process_status', return_value={'status': 'alive_same'}):
            result_cutover.prepare(self.db, now=self.now)
        for status, owner in (('unknown', None), ('dead', {'round_id': 'round'})):
            with self.subTest(status=status), patch.object(run_once, '_dispatch_process_status', return_value={'status': status}), patch.object(native_rounds, 'owner', return_value=(None, owner)), patch.object(run_once, '_terminate_attempt_process') as terminate:
                self.assertTrue(result_cutover.prepare(self.db, now=self.now)['ok'])
                terminate.assert_not_called()
                with closing(sqlite3.connect(self.db)) as conn:
                    self.assertEqual(conn.execute("SELECT status FROM attempts WHERE task_id='bad'").fetchone()[0], 'running')
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 1)

    def test_exit_observation_does_not_overwrite_a_changed_attempt(self):
        with patch.object(run_once, '_dispatch_process_status', return_value={'status': 'alive_same'}):
            result_cutover.prepare(self.db, now=self.now)
        def process_changed(metadata):
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE attempts SET metadata_json=json_set(metadata_json,'$.dispatch.pid',99999999) WHERE task_id='bad'")
            return {'status': 'dead'}
        with patch.object(run_once, '_dispatch_process_status', side_effect=process_changed):
            result_cutover.prepare(self.db, now=self.now)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM attempts WHERE task_id='bad'").fetchone()[0], 'running')

    def test_main_board_retrigger_route_creates_one_recovery_and_hides_pending_action(self):
        from robert_agent import discover, web
        self.github_item()
        with patch.object(run_once, '_dispatch_process_status', return_value={'status': 'dead'}):
            result_cutover.prepare(self.db, now=self.now)
            context = web.ControlContext(str(self.db), 'owner', frozenset({'repo'}), frozenset(), frozenset({'http://127.0.0.1:8765'}), 'csrf', True, None)
            headers = {'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765', 'Content-Type': 'application/json', 'X-Robert-CSRF-Token': 'csrf', 'X-Idempotency-Key': 'board-recovery'}
            self.assertIn('retrigger', board.get_work_item_detail(self.db, 'item')['valid_commands'])
            with patch.object(discover, '_source_metadata', return_value=({'body': 'fresh source'}, 'open')):
                status, _, body = web.handle_dashboard_post('/api/tasks/bad/retrigger', b'{}', self.db, control_context=context, request_headers=headers)
            self.assertEqual(status, 202, body)
            self.assertTrue(json.loads(body)['ok'])
        self.assertEqual(board.get_work_item_detail(self.db, 'item')['valid_commands'], ['cancel'])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM work_item_events WHERE event_type='execution_recovery_requested'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM wakeups WHERE status='pending'").fetchone()[0], 1)

    def test_completion_during_pause_preserves_global_latch_and_notification(self):
        from unittest.mock import Mock
        context = self.context()
        daemon._record_control_outcome(context, 'scan', {'ok': False, 'status': 'blocked_result_contract_cutover'}, now=self.now)
        lane = Mock()
        lane.poll.return_value = {'ok': True, 'status': 'done'}
        context.background_lanes = {'scan': lane}
        with patch.object(result_cutover, 'check', side_effect=AssertionError('must remain paused')), patch.object(daemon, '_maybe_prune'), patch.object(daemon.notify, 'deliver_pending'):
            self.assertEqual(daemon.run_once_decision(context, now=self.now)['decision'], 'control_attention')
        self.assertEqual(context.control_attention['scan']['cause_status'], 'blocked_result_contract_cutover')
        lane.start.assert_not_called()
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM notifications WHERE notification_type='control_plane_failed'").fetchone()[0], 'recorded')


class RuntimeAndContextTests(unittest.TestCase):
    def test_internal_helpers_use_running_robert_even_with_old_config_override(self):
        import sys
        from robert_agent import render_prompt, route
        with patch.object(sys, 'executable', '/opt/current/bin/python'):
            runtime = run_once._runtime_context('/tmp/db', {'python_bin': '/opt/old/bin/python', 'default_base_branch': 'main'})
            prompt = render_prompt.render_prompt({'task_id': 'task', 'attempt_id': 'attempt', 'workstream_id': 'ws'},
                route.ROUTES['classification-result'], [], runtime_context={**runtime, 'python_bin': '/opt/old/bin/python'})
        self.assertEqual(runtime['python_bin'], '/opt/current/bin/python')
        self.assertIn('/opt/current/bin/python', prompt)
        self.assertNotIn('/opt/old/bin/python', prompt)
