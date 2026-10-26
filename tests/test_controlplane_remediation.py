from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests import test_daemon as daemon_fixture
from tests import test_retention as retention_fixture
from tests import test_operational_commands as publication_fixture
from robert_agent import daemon, daemon_state, discover, notify, publish, result_spool, route, run_once, storage
from robert_agent.validate_config import DAEMON_DEFAULTS


class ControlplaneRemediationTests(unittest.TestCase):
    def test_control_deadline_aborts_scope_observation_without_rejecting_result(self):
        fixture = publication_fixture.OperationalCommandTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        db = fixture.root / 'dd.sqlite3'
        fixture._init_summary_db(db)
        with mock.patch.dict(os.environ, {'_ROBERT_CONTROL_DEADLINE': '0'}):
            with closing(sqlite3.connect(db)) as conn:
                with self.assertRaises(subprocess.TimeoutExpired):
                    run_once._action_scope_for_result(conn, 'task-1', 'attempt-1', 'ws-1')
                self.assertEqual(conn.execute('SELECT audit_status FROM github_actions').fetchone()[0], 'accepted')

    def test_expired_control_deadline_prevents_audit_and_recovery_git_calls(self):
        fixture = publication_fixture.OperationalCommandTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        db = fixture.root / 'dd.sqlite3'
        fixture._init_summary_db(db)
        with mock.patch.dict(os.environ, {'_ROBERT_CONTROL_DEADLINE': '0'}):
            with mock.patch.object(run_once.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as runner:
                with self.assertRaises(subprocess.TimeoutExpired):
                    run_once._run_inspection_command(['git', 'status', '--short'], fixture.root)
                with closing(sqlite3.connect(db)) as conn:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        run_once._action_scope_for_result(conn, 'task-1', 'attempt-1', 'ws-1')
                runner.assert_not_called()

    def test_publication_requires_recorded_worker_process_stop(self):
        fixture = publication_fixture.OperationalCommandTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        db = fixture.root / 'dd.sqlite3'
        fixture._init_summary_db(db)
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=json_remove(metadata_json,'$.process_stopped_at')")
        self.assertEqual(publish.publish_ready_actions(db, dry_run=True)['pending_count'], 0)
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=json_set(metadata_json,'$.process_stopped_at',?)",
                         (datetime.now(timezone.utc).isoformat(),))
        self.assertEqual(publish.publish_ready_actions(db, dry_run=True)['pending_count'], 1)

    def test_notification_delivery_queries_only_pending_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'db.sqlite3'
            storage.init_database(db)
            now = '2026-10-26T00:00:00+00:00'
            sql = """SELECT notification_id FROM notifications WHERE channel='local'
                AND status NOT IN ('resolved','sent')
                AND COALESCE(json_extract(metadata_json,'$.delivery.status'),'') NOT IN ('sent','exhausted')
                ORDER BY created_at,notification_id LIMIT 100"""
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.executemany("""INSERT INTO notifications(notification_id,notification_type,
                    channel,status,created_at,metadata_json) VALUES (?,'notice','local','recorded',?,?)""",
                    [(str(i), now, '{"delivery":{"status":"sent"}}') for i in range(1000)])
                conn.execute("""INSERT INTO notifications(notification_id,notification_type,
                    channel,status,created_at) VALUES ('pending','notice','local','recorded',?)""", (now,))
                plan = ' '.join(row[3] for row in conn.execute('EXPLAIN QUERY PLAN ' + sql))
                self.assertIn('USING INDEX', plan)
            received = []
            def receiver(*args, **kwargs):
                received.append(json.loads(kwargs['input']))
                return subprocess.CompletedProcess(args, 0, '', '')
            self.assertEqual(notify.deliver_pending(db, command=['receiver'], runner=receiver, now=now)['sent'], 1)
            self.assertEqual(len(received), 1)
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute(sql).fetchall(), [])
                self.assertEqual(conn.execute('SELECT count(*) FROM notifications').fetchone()[0], 1001)

    def test_analysis_and_implementation_intents_do_not_match_substrings(self):
        for body in ['分析一下为什么失败', 'please explain this bug', 'analyze fixture behavior']:
            with self.subTest(body=body):
                self.assertEqual(discover.infer_intent('', body, 'comment'), 'analysis')
        for body in ['prefix', 'fixture', 'suffix', 'debug information']:
            with self.subTest(body=body):
                self.assertNotEqual(discover.infer_intent('', body, 'comment'), 'bug_fix')
        for body in ['实现用户登录', '支持增量同步', 'add a feature', 'implement pagination']:
            with self.subTest(body=body):
                intent = discover.infer_intent('', body, 'comment')
                self.assertEqual(route.route_task({'intent': intent, 'source_type': 'issue'})['route_id'], 'new-pr')

    def test_search_floor_does_not_disable_core_only_publication(self):
        fixture = daemon_fixture.DaemonSchedulingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        config = fixture._write_config()
        storage.init_database(fixture.db_path)
        now = '2026-07-03T00:00:00+00:00'
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            fixture._insert_repo(conn)
            conn.execute('''INSERT INTO wakeups(wakeup_id,repo_id,reason,dedupe_key,
                status,not_before_at,created_at,updated_at) VALUES (
                'w','repo-1','manual_operator_request','k','pending',?,?,?)''', (now, now, now))
        calls = []
        def child(command, **kwargs):
            calls.append(command)
            return {'ok': True, 'status': 'completed'}
        def rates(*args, **kwargs):
            return subprocess.CompletedProcess(args, 0, json.dumps({'resources': {
                'core': {'remaining': 5000}, 'search': {'remaining': 0}}}), '')
        daemon.run_daemon(config, once=True, child_runner=child, rate_limit_runner=rates, now=now)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('--skip-publish', calls[0])

    def test_rejected_spool_prefix_cannot_permanently_starve_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'db.sqlite3'
            directory = result_spool.directory(db)
            directory.mkdir()
            import hashlib
            entries = sorted((hashlib.sha256(str(i).encode()).hexdigest(), str(i)) for i in range(3))
            for i, (key, attempt) in enumerate(entries):
                payload = {'attempt_id': attempt, 'task_id': 'task'}
                record = {'status': 'pending' if i == 2 else 'rejected', 'payload': payload,
                          'fingerprint': result_spool.fingerprint(payload)}
                (directory / (key + '.json')).write_text(json.dumps(record))
            received = []
            for _ in range(3):
                with mock.patch.object(result_spool.time, 'monotonic', side_effect=[0, 0, 3]):
                    result_spool.replay(db, lambda *args, **kwargs: received.append(True) or {'ok': True, 'status': 'recorded'})
            self.assertEqual(received, [True])
            self.assertEqual(len(list((directory / 'rejected').glob('*.json'))), 2)

    def test_maintenance_catches_up_more_than_one_event_batch(self):
        fixture = retention_fixture.RetentionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for i in range(250):
            fixture.add_event('old-' + str(i))
        with mock.patch.object(daemon.time, 'monotonic', return_value=1):
            daemon._maybe_prune(fixture.context, now=retention_fixture.NOW)
        with fixture.connect() as conn:
            remaining = conn.execute("SELECT count(*) FROM daemon_events WHERE event_type='poll'").fetchone()[0]
        self.assertLess(remaining, 150)

    def test_timed_out_control_child_releases_only_its_own_run_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'db.sqlite3'
            ready = Path(tmp) / 'child-ready'
            storage.init_database(db)
            now = datetime.now(timezone.utc).isoformat()
            with closing(daemon_state.connect(db)) as conn, conn:
                daemon_state.start_daemon_run(conn, config_path='/config', owner_id='daemon', now=now)
                conn.execute("INSERT INTO agent_runs(run_id,status,started_at) VALUES ('unrelated','running',?)", (now,))
            program = '''import json,os,sqlite3,sys,time
from datetime import datetime,timezone
from pathlib import Path
from robert_agent import run_once
now=datetime.now(timezone.utc).isoformat()
conn=sqlite3.connect(sys.argv[1])
conn.execute("INSERT INTO agent_runs(run_id,status,started_at,summary_json) VALUES ('timed','running',?,?)",
             (now,json.dumps({'executor_id':os.environ.get('_ROBERT_EXECUTOR_ID')})))
run_once._acquire_agent_lease(conn,'repo:x','timed',now,9)
conn.commit()
Path(sys.argv[2]).touch()
time.sleep(30)
'''
            context = daemon.DaemonContext('/config', None, db, 'repo:x', 1,
                {**DAEMON_DEFAULTS, 'local_drain_timeout_seconds': .3}, 'daemon', 'lease')
            command = [sys.executable, '-B', '-c', program, str(db), str(ready)]
            original_popen = subprocess.Popen

            def start_ready_child(argv, *args, **kwargs):
                process = original_popen(argv, *args, **kwargs)
                if argv != command:
                    return process

                def cleanup():
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                    for stream in (process.stdout, process.stderr):
                        if stream:
                            stream.close()

                self.addCleanup(cleanup)
                # Start the cleanup deadline only after the fixture run/lease
                # exists; interpreter startup can exceed 0.3s on loaded CI.
                deadline = time.monotonic() + 10
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists(), 'control child did not persist its run and lease')
                return process

            with mock.patch.object(daemon, 'build_loop_command', return_value=command), \
                    mock.patch.object(daemon.subprocess, 'Popen', side_effect=start_ready_child):
                response = daemon._run_local_drain(context)
            self.assertFalse(response['ok'])
            self.assertEqual(response['status'], 'child_timeout')
            self.assertTrue(response['child']['process_group_stopped'])
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute("SELECT status FROM agent_runs WHERE run_id='timed'").fetchone()[0], 'failed')
                self.assertEqual(conn.execute("SELECT status FROM leases WHERE owner_id='timed'").fetchone()[0], 'released')
                self.assertEqual(conn.execute("SELECT status FROM agent_runs WHERE run_id='unrelated'").fetchone()[0], 'running')


if __name__ == '__main__':
    unittest.main()
