from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests import support  # noqa: F401
from robert_agent import storage
from robert_agent.worker import heartbeat


class ExecutionJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'state.sqlite3'
        storage.init_database(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES('r','x/y','bot','main','/repo','/repo/w')")
            conn.execute("INSERT INTO workstreams(workstream_id,repo_id,lifecycle,created_at,updated_at) VALUES('w','r','active','now','now')")
            conn.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,priority,created_at,updated_at) VALUES('t','w','running','P1','now','now')")
            conn.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status,started_at,heartbeat_at) VALUES('a','t',1,'running','now','now')")

    def run_command(self, script, **kwargs):
        return heartbeat.run_command_with_heartbeat(
            self.db, 't', 'a', 'verify', [sys.executable, '-c', script],
            heartbeat_interval_seconds=.1, **kwargs,
        )

    def cli(self, script, *options):
        return [sys.executable, '-m', 'robert_agent.worker.heartbeat',
                '--db', str(self.db), '--task-id', 't', '--attempt-id', 'a',
                '--phase', 'verify', '--heartbeat-interval-seconds', '.1',
                *options, '--', sys.executable, '-c', script]

    def test_journal_binds_actual_argv_and_cwd_without_claiming_attestation(self):
        script = "from pathlib import Path; print(Path.cwd())"
        result = self.run_command(script, cwd=self.root)
        record = json.loads(Path(result['command_record_path']).read_text())
        self.assertEqual(record['argv'], [sys.executable, '-c', script])
        self.assertEqual(record['cwd'], str(self.root.resolve()))
        self.assertEqual(result['stdout_tail'].strip(), str(self.root.resolve()))
        self.assertEqual(record['source'], 'runtime_recorded')
        self.assertFalse(record['attested'])
        self.assertFalse(record['argv_redacted'])
        self.assertEqual(record['output_mode'], 'summary')

    def test_sensitive_arguments_are_redacted_without_changing_execution(self):
        command = [sys.executable, '-c', "import sys; print(len(sys.argv))",
                   '--token', 'short', '--password=abc', 'API_KEY=private-value',
                   'https://alice:private@host/path', '-H', 'Authorization: Bearer private']
        result = heartbeat.run_command_with_heartbeat(self.db, 't', 'a', 'verify', command)
        record = json.loads(Path(result['command_record_path']).read_text())
        self.assertEqual(record['argv'][:3], command[:3])
        for index in (4, 5, 6, 7, 9):
            self.assertEqual(record['argv'][index], '<redacted>')
        self.assertTrue(record['argv_redacted'])
        self.assertIn('8', result['stdout_tail'])
        self.assertEqual(os.stat(result['command_record_path']).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(result['stdout_path']).st_mode & 0o777, 0o600)

    def test_summary_is_default_and_tee_preserves_binary_streams_without_json(self):
        script = "import os; os.write(1,b'out\\x00\\xff'); os.write(2,b'err\\xfe'); raise SystemExit(7)"
        summary = subprocess.run(self.cli(script), capture_output=True, check=False)
        self.assertEqual(summary.returncode, 0)
        self.assertEqual(json.loads(summary.stdout)['returncode'], 7)
        self.assertEqual(summary.stderr, b'')
        tee = subprocess.run(self.cli(script, '--output-mode', 'tee'), capture_output=True, check=False)
        self.assertEqual(tee.returncode, 7)
        self.assertEqual(tee.stdout, b'out\x00\xff')
        self.assertEqual(tee.stderr, b'err\xfe')
        records = [json.loads(p.read_text()) for p in (self.root / 'artifacts/t').glob('*.command.json')]
        record = next(r for r in records if r.get('output_mode') == 'tee')
        self.assertEqual(Path(record['stdout_path']).read_bytes(), tee.stdout)
        self.assertEqual(Path(record['stderr_path']).read_bytes(), tee.stderr)

    def test_tee_forwards_before_command_finishes_and_flushes_large_output(self):
        script = "import os,sys; print('ready',flush=True); sys.stdin.readline(); os.write(1,b'A'*200000); os.write(2,b'B'*200000)"
        proc = subprocess.Popen(self.cli(script, '--output-mode', 'tee'),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertEqual(proc.stdout.readline(), b'ready\n')
        stdout, stderr = proc.communicate(b'continue\n', timeout=10)
        self.assertEqual(stdout, b'A'*200000)
        self.assertEqual(stderr, b'B'*200000)
        self.assertEqual(proc.returncode, 0)

    def test_tee_timeout_keeps_partial_output_and_durable_record(self):
        proc = subprocess.run(self.cli("import time; print('partial',flush=True); time.sleep(30)",
                                      '--output-mode', 'tee', '--timeout-seconds', '1'),
                              capture_output=True, timeout=10, check=False)
        self.assertEqual(proc.returncode, 124)
        self.assertEqual(proc.stdout, b'partial\n')
        record = json.loads(next((self.root / 'artifacts/t').glob('*.command.json')).read_text())
        self.assertTrue(record['timed_out'])
        self.assertEqual(record['status'], 'failed')

    def test_command_does_not_hold_database_transaction(self):
        script = ("import sqlite3; c=sqlite3.connect(" + repr(str(self.db)) + "); "
                  "c.execute('BEGIN IMMEDIATE'); c.rollback(); print('unlocked')")
        with patch.object(heartbeat.snapshot, 'record_snapshot', side_effect=sqlite3.OperationalError('database is locked')):
            result = self.run_command(script, output_mode='tee')
        self.assertEqual(result['returncode'], 0)
        self.assertEqual(result['snapshot_status'], 'deferred')

    def test_tee_backpressure_does_not_prevent_timeout(self):
        proc = subprocess.Popen(self.cli("import os,time; os.write(1,b'A'*2000000); time.sleep(30)",
                                         '--output-mode', 'tee', '--timeout-seconds', '1'),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertEqual(proc.wait(timeout=5), 124)
        stdout, stderr = proc.communicate(timeout=5)
        self.assertEqual(stderr, b'')
        record = json.loads(next((self.root / 'artifacts/t').glob('*.command.json')).read_text())
        self.assertTrue(record['tee_incomplete'])
        self.assertTrue(record['timed_out'])
        self.assertEqual(Path(record['stdout_path']).read_bytes(), b'A'*2000000)
        self.assertLess(len(stdout), 2000000)
