from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests import test_daemon as daemon_fixture
from robert_agent import daemon, daemon_state, discover, route, run_once, storage
from robert_agent.validate_config import DAEMON_DEFAULTS


class FollowupDaemonTests(unittest.TestCase):
    def _group_probe(self, mode):
        original = run_once._live_process_group

        def inspect(pgid):
            def ps(command, **kwargs):
                self.assertEqual(command, ['ps', '-axo', 'pid=,pgid=,stat='])
                self.assertEqual(kwargs['timeout'], 5)
                if mode == 'oserror':
                    raise OSError('ps unavailable')
                if mode == 'timeout':
                    raise subprocess.TimeoutExpired(command, 5)
                return subprocess.CompletedProcess(command, 1, '', 'unsupported ps option')

            # Enter after run_child_json selects its real Popen path.
            with mock.patch.object(subprocess, 'run', side_effect=ps):
                return original(pgid)

        return inspect

    def _run_owned_child(self, mode, *, slow):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / 'dd.sqlite3'
            ready = root / 'ready'
            storage.init_database(db)
            now = datetime.now(timezone.utc).isoformat()
            with closing(daemon_state.connect(db)) as conn, conn:
                daemon_state.start_daemon_run(conn, config_path='/config', owner_id='daemon', now=now)
            program = '''import json,os,sqlite3,sys,time
from pathlib import Path
from robert_agent import run_once
conn=sqlite3.connect(sys.argv[1])
conn.execute("INSERT INTO agent_runs(run_id,status,started_at,summary_json) VALUES ('owned','running',?,?)",
             (sys.argv[3],json.dumps({'executor_id':os.environ['_ROBERT_EXECUTOR_ID']})))
run_once._acquire_agent_lease(conn,'repo:x','owned',sys.argv[3],90)
conn.commit()
Path(sys.argv[2]).touch()
if sys.argv[4]=='slow': time.sleep(30)
print(json.dumps({'ok':True,'status':'completed','marker':'real-child'}))
'''
            command = [sys.executable, '-B', '-c', program, str(db), str(ready), now,
                       'slow' if slow else 'fast']
            original_popen = subprocess.Popen
            processes = []

            def launch(*args, **kwargs):
                process = original_popen(*args, **kwargs)
                processes.append(process)

                def cleanup():
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                    for stream in (process.stdout, process.stderr):
                        if stream:
                            stream.close()

                self.addCleanup(cleanup)
                deadline = time.monotonic() + 10
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists(), 'child did not save its run and lease')
                return process

            context = daemon.DaemonContext('/config', None, db, 'repo:x', 1,
                {**DAEMON_DEFAULTS, 'local_drain_timeout_seconds': .1 if slow else 3}, 'daemon', 'lease')
            with mock.patch.object(daemon, 'build_loop_command', return_value=command), \
                    mock.patch.object(daemon.subprocess, 'Popen', side_effect=launch), \
                    mock.patch.object(run_once, '_live_process_group', side_effect=self._group_probe(mode)):
                result = daemon._run_local_drain(context)
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].poll())
            self.assertFalse(result['child']['process_group_stopped'])
            self.assertTrue(result['child']['executor_id'])
            if slow:
                self.assertFalse(result['ok'])
                self.assertEqual(result['status'], 'child_timeout')
                self.assertNotEqual(processes[0].returncode, 0)
            else:
                self.assertTrue(result['ok'])
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(result['marker'], 'real-child')
                self.assertEqual(result['child']['returncode'], 0)
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute("SELECT status FROM agent_runs WHERE run_id='owned'").fetchone()[0], 'running')
                self.assertEqual(conn.execute("SELECT status FROM leases WHERE owner_id='owned'").fetchone()[0], 'active')

    def test_group_observation_failure_preserves_real_success_and_ownership(self):
        for mode in ('nonzero', 'oserror', 'timeout'):
            with self.subTest(mode=mode):
                self._run_owned_child(mode, slow=False)

    def test_group_observation_failure_preserves_real_timeout_and_ownership(self):
        for mode in ('nonzero', 'oserror', 'timeout'):
            with self.subTest(mode=mode):
                self._run_owned_child(mode, slow=True)

    def test_real_launch_failure_is_not_hidden_as_group_observation_failure(self):
        with mock.patch.object(daemon.subprocess, 'Popen', side_effect=OSError('fork failed')):
            with self.assertRaisesRegex(OSError, 'fork failed'):
                daemon.run_child_json(['control'], timeout_seconds=1)

    def test_startup_with_unavailable_group_proof_keeps_abandoned_run_and_lease(self):
        for mode in ('nonzero', 'oserror', 'timeout', 'stopped'):
            with self.subTest(mode=mode):
                fixture = daemon_fixture.DaemonSchedulingTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                config = fixture._write_config()
                fixture._init_db()
                now = datetime.now(timezone.utc).isoformat()
                identity = {'pid': 999991, 'process_group_id': 999991}
                with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
                    conn.execute("INSERT INTO agent_runs(run_id,status,started_at,summary_json) VALUES ('abandoned','running',?,?)",
                                 (now, json.dumps({'executor_id': 'executor', 'executor_identity': identity})))
                    run_once._acquire_agent_lease(conn, 'repo:x', 'abandoned', now, 90)
                probe = (lambda _pid: False) if mode == 'stopped' else self._group_probe(mode)
                with mock.patch.object(daemon.worker_identity, 'inspect_recorded_identity', return_value='dead'), \
                        mock.patch.object(run_once, '_live_process_group', side_effect=probe):
                    result = daemon.run_daemon(config, once=True)
                self.assertTrue(result['ok'], result)
                self.assertEqual(result['status'], 'stopped')
                with closing(sqlite3.connect(fixture.db_path)) as conn:
                    self.assertEqual(conn.execute("SELECT status FROM agent_runs WHERE run_id='abandoned'").fetchone()[0],
                                     'failed' if mode == 'stopped' else 'running')
                    self.assertEqual(conn.execute("SELECT status FROM leases WHERE owner_id='abandoned'").fetchone()[0],
                                     'released' if mode == 'stopped' else 'active')
                    self.assertEqual(conn.execute('SELECT status FROM daemon_runs').fetchone()[0], 'stopped')
                    self.assertEqual(conn.execute("SELECT status FROM leases WHERE resource_type='daemon'").fetchone()[0], 'released')


class FollowupIntentTests(unittest.TestCase):
    def test_mixed_bug_reports_retain_implementation_route(self):
        for title, body in [
            ('登录失败', '报错 500，期望方案：恢复正常登录'),
            ('Crash on save', 'Can someone explain why? Please fix it.'),
            ('Bug: login broken', "let's discuss the fix"),
        ]:
            with self.subTest(title=title):
                event = discover._issue_search_to_raw(
                    {'number': 1, 'title': title, 'body': body, 'author': {'login': 'wklken'}}, 'issue', 'robot')
                self.assertEqual(event['intent'], 'bug_fix')
                policy = route.route_task({'intent': event['intent'], 'source_type': 'issue'})
                self.assertEqual(policy['route_id'], 'new-pr')
                self.assertTrue(policy['needs_worktree'])

    def test_explicit_requests_boundaries_and_negation_keep_their_intents(self):
        for title, body, event_type, expected in [
            ('Bug: login failed', '分析一下为什么失败', 'issue', 'analysis'),
            ('', 'please explain this bug', 'comment', 'analysis'),
            ('', 'analyze fixture behavior', 'comment', 'analysis'),
            ('', 'Please fix this bug; discuss the root cause afterward.', 'issue', 'bug_fix'),
            ('', 'prefix', 'issue', 'unclear'),
            ('', 'fixture', 'issue', 'unclear'),
            ('', 'suffix', 'issue', 'unclear'),
            ('', 'debug information', 'issue', 'unclear'),
            ('', 'do not fix this bug', 'comment', 'unclear'),
            ('', '不要修复这个失败，讨论一下方案', 'comment', 'analysis'),
            ('Bug: login failed', '@robot 投票：选择方案 A 或 B', 'comment', 'vote'),
            ('Bug: login failed', '@robot please review this', 'comment', 'review_request'),
            ('Bug: login failed', 'please fix this', 'review_request', 'review_request'),
        ]:
            with self.subTest(body=body):
                self.assertEqual(discover.infer_intent(title, body, event_type, 'robot'), expected)


if __name__ == '__main__':
    unittest.main()
