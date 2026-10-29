from contextlib import closing
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import support  # noqa: F401
from robert_agent import daemon, dispatch, execution_store, loop_engine
from robert_agent import review_store, run_once, validate_config, vote_store


class LaunchOwnershipTests(unittest.TestCase):
    def test_failed_launch_exposes_unconfirmed_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / 'prompt.md'
            prompt.write_text('test')
            with patch.object(dispatch.subprocess, 'Popen', return_value=SimpleNamespace(pid=987654, poll=lambda: 2)), \
                 patch.object(dispatch.worker_identity, 'cleanup_new_process', return_value=False):
                result = dispatch.dispatch_worker('task', 'attempt', prompt, dry_run=False, startup_probe_seconds=0)
            self.assertTrue(result['cleanup_pending'])
            self.assertTrue(result['process_exited'])

    def test_cleanup_attention_can_only_be_canceled_after_group_is_gone(self):
        metadata = {'dispatch': {'pid': 987654, 'cleanup_pending': True}}
        with patch.object(run_once.worker_identity, 'terminate_recorded_identity', side_effect=AssertionError('unverified PID must not be signaled')):
            with patch.object(run_once, '_live_process_group', return_value=True):
                self.assertEqual(run_once._terminate_attempt_process(metadata)['status'], 'attention')
            with patch.object(run_once, '_live_process_group', return_value=False):
                self.assertEqual(run_once._terminate_attempt_process(metadata)['status'], 'already_dead')

    def test_failed_leader_cleans_up_term_resistant_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / 'prompt.md'
            prompt.write_text('test')
            ready = root / 'ready'
            child = ("import signal,time; from pathlib import Path; "
                     "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                     f"Path({str(ready)!r}).touch(); time.sleep(60)")
            leader = ("import subprocess,sys,time; from pathlib import Path; "
                      f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
                      f"ready=Path({str(ready)!r})\n"
                      "while not ready.exists(): time.sleep(.01)\n"
                      "sys.exit(2)")
            popen = subprocess.Popen
            processes = []

            def launch(*args, **kwargs):
                proc = popen(*args, **kwargs)
                if kwargs.get('start_new_session'):
                    processes.append(proc)
                    proc.wait(timeout=5)  # Deterministic startup-probe failure.
                return proc

            try:
                with patch.object(dispatch.subprocess, 'Popen', side_effect=launch):
                    result = dispatch.dispatch_worker('task', 'attempt', prompt,
                        worker_agent_config={'agent': 'command', 'command_argv': [sys.executable, '-c', leader]},
                        dry_run=False, startup_probe_seconds=0)
                self.assertFalse(result['ok'])
                self.assertFalse(run_once._live_process_group(result['pid']))
                self.assertFalse(result.get('cleanup_pending', False))
            finally:
                for proc in processes:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait(timeout=5)

    def test_unconfirmed_launch_cleanup_retains_capacity_and_attention(self):
        from tests.test_run_once import RunOnceDryRunTests
        fixture = RunOnceDryRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        first = run_once.run_once(fixture.config_path, fixture_path=fixture.fixture_path,
                                   dry_run=True, skip_external=True, skip_publish=True)
        self.assertTrue(first['ok'], first)
        with patch.object(run_once.dispatch, 'dispatch_worker', return_value={
            'ok': False, 'status': 'failed_dispatch', 'pid': 987654,
            'cleanup_pending': True, 'process_exited': True, 'returncode': 2,
            'safe_error': 'worker process group cleanup was not confirmed',
        }):
            result = run_once.run_once(fixture.config_path, dry_run=False,
                                       skip_external=True, skip_publish=True)
        self.assertFalse(result['ok'])
        with closing(sqlite3.connect(fixture.data_dir / 'dd.sqlite3')) as conn:
            status, task_id = conn.execute('SELECT status,task_id FROM attempts').fetchone()
            self.assertEqual(status, 'stale')
            self.assertEqual(run_once._running_attempt_count_for_repos(
                conn, [row[0] for row in conn.execute('SELECT repo_id FROM repos')]), 1)
            self.assertEqual(execution_store.get_binding(conn, 'task', task_id)['binding_status'], 'attention')
            self.assertIn(conn.execute('SELECT lifecycle FROM tasks').fetchone()[0], ('queued', 'running'))


class RunDirectoryRecoveryTests(unittest.TestCase):
    def test_rollback_orphan_is_not_reused_or_allowed_to_block_retry(self):
        for store, kind in ((review_store, 'review'), (vote_store, 'vote')):
            for stage in ('prepare', kind, 'synthesis'):
                with self.subTest(kind=kind, stage=stage), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    db = root / 'state.sqlite3'
                    with closing(sqlite3.connect(db)) as conn, conn:
                        conn.execute('CREATE TABLE tasks(task_id TEXT PRIMARY KEY)')
                        conn.execute('CREATE TABLE attempts(attempt_id TEXT PRIMARY KEY)')
                        conn.execute("INSERT INTO attempts VALUES ('attempt')")
                        store.initialize(conn)
                        request = store.enqueue(conn, {'full_name': 'example/repo'},
                            {'number': 1, 'source_type': 'issue', 'workstream_id': 'ws', 'event_fingerprint': 'trigger'}, {}, [])
                    with self.assertRaisesRegex(RuntimeError, 'rollback'):
                        with store.connect(db) as conn:
                            old = store.new_run(conn, request['round_id'], stage, 'worker', 'attempt', root, 'hash')
                            Path(old['output_dir'], 'result.json').write_text('untrusted orphan result')
                            raise RuntimeError('rollback')
                    with store.connect(db) as conn:
                        new = store.new_run(conn, request['round_id'], stage, 'worker', 'attempt', root, 'hash')
                    self.assertEqual(new['attempt_no'], 1)
                    self.assertNotEqual(old['output_dir'], new['output_dir'])
                    self.assertEqual(list(Path(new['output_dir']).iterdir()), [])
                    self.assertEqual(Path(old['output_dir'], 'result.json').read_text(), 'untrusted orphan result')


class IdleSchedulingTests(unittest.TestCase):
    def test_fractional_deadline_waits_instead_of_spinning(self):
        now = datetime.now(timezone.utc)
        context = SimpleNamespace(daemon_config={'local_poll_seconds': 5}, background_lanes=None,
            next_live_poll_at=(now + timedelta(seconds=.4)).isoformat())
        waits = []
        daemon._wait_for_next_decision(context, {'decision': 'idle'}, now=now, sleeper=waits.append)
        self.assertEqual(waits, [.4])

    def fixture(self):
        from tests.test_scheduler_remediation import SchedulerRemediationTests
        fixture = SchedulerRemediationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.config.write_text(fixture.config.read_text().replace('max_concurrency: 3', 'max_concurrency: 1'))
        now = fixture.now.isoformat()
        with fixture.connect() as conn:
            conn.execute("INSERT INTO workstreams(workstream_id,repo_id,lifecycle,created_at,updated_at) VALUES ('waiting','repo:example/repo','active',?,?)", (now, now))
            conn.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES ('waiting','waiting','queued',?,?)", (now, now))
            conn.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status,started_at,heartbeat_at) VALUES ('waiting','waiting',1,'prepared',?,?)", (now, now))
        return fixture

    def test_full_capacity_prepared_work_does_not_create_runs(self):
        fixture = self.fixture()
        result = loop_engine.run_loop(fixture.config, dry_run=False, skip_external=True, skip_publish=True)
        self.assertEqual(result['cycles'], 0)
        with fixture.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM agent_runs').fetchone()[0], 0)

    def test_capacity_gate_preserves_supervision_and_manual_wakeups(self):
        fixture = self.fixture()
        config = validate_config.validate_config(fixture.config, skip_external=True)
        self.assertFalse(fixture.runnable(config_result=config))
        fixture.stop_process()
        self.assertTrue(fixture.runnable(config_result=config))
        with patch.object(run_once, '_dispatch_process_status', return_value={'status': 'alive_same'}):
            with fixture.connect() as conn:
                now = fixture.now.isoformat()
                conn.execute("INSERT INTO wakeups(wakeup_id,repo_id,reason,dedupe_key,status,not_before_at,created_at,updated_at) VALUES ('manual','repo:example/repo','manual_operator_request','manual','pending',?,?,?)", (now, now, now))
            self.assertTrue(fixture.runnable(config_result=config))

    def test_repo_capacity_does_not_block_another_repository(self):
        fixture = self.fixture()
        config = validate_config.validate_config(fixture.config, skip_external=True)
        config['max_concurrency'] = 2
        config['repos'][0]['max_concurrency'] = 1
        self.assertFalse(fixture.runnable(config_result=config))
        with fixture.connect() as conn:
            conn.execute("INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('other','example/other','robot','main','/repo','/worktrees')")
            conn.execute("UPDATE workstreams SET repo_id='other' WHERE workstream_id='waiting'")
            self.assertTrue(loop_engine.has_runnable_local_work(conn,
                repo_ids=['repo:example/repo', 'other'], config_result=config))

