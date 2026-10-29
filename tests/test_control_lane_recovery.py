from tests import support  # noqa: F401
from contextlib import closing, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from robert_agent import control_lane, daemon_state, storage, worker_identity


OLD_BOOT = {'platform': 'linux', 'boot_source': 'linux_boot_id', 'boot_id': 'old-boot', 'pid_namespace': 'pid:[100]'}
NEW_BOOT = {'platform': 'linux', 'boot_source': 'linux_boot_id', 'boot_id': 'new-boot', 'pid_namespace': 'pid:[200]'}


class ControlLaneRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / 'state.sqlite3'
        storage.init_database(self.db)

    def child(self, *, context=OLD_BOOT, active=False, identity=None):
        when = '2099-01-01T00:00:00+00:00' if active else '2020-01-01T00:00:00+00:00'
        recorded = identity or {'pid': 999999, 'process_group_id': 999999}
        if context is not None:
            recorded = {**recorded, 'execution_context': context}
        with closing(sqlite3.connect(self.db)) as conn, conn:
            daemon_state.start_daemon_run(conn, config_path='/config', owner_id='old', now=when)
            daemon_state.acquire_daemon_lease(conn, resource_key='scope', owner_id='old', ttl_seconds=60, now=when)
            conn.execute("INSERT INTO github_control_children VALUES ('child','old','scan',?,'running')", (json.dumps(recorded),))

    def state(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute("SELECT status FROM github_control_children WHERE child_id='child'").fetchone()[0]

    def test_different_boot_retires_record_without_signaling_any_pid(self):
        self.child()
        with mock.patch.object(control_lane, '_boot_context', return_value=NEW_BOOT), \
             mock.patch.object(worker_identity, 'terminate_recorded_identity', side_effect=AssertionError('must not signal new boot PID')):
            self.assertTrue(control_lane.recover(self.db, scope_key='scope', current_owner='new'))
        self.assertEqual(self.state(), 'stopped')
        with closing(sqlite3.connect(self.db)) as conn:
            evidence = json.loads(conn.execute("SELECT metadata_json FROM daemon_events WHERE event_type='control_child_retired'").fetchone()[0])
        self.assertEqual(evidence['resolution'], 'different_boot')

    def test_same_boot_reused_pid_is_not_signaled_or_automatically_retired(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        self.addCleanup(cleanup)
        recorded = worker_identity.capture_process_identity(process.pid)
        recorded['process_start_token'] = 'prior-process-start-token'
        self.child(identity=recorded)
        with mock.patch.object(control_lane, '_boot_context', return_value={**OLD_BOOT, 'pid_namespace': 'pid:[200]'}), \
             mock.patch.object(os, 'killpg', side_effect=AssertionError('unrelated process must not be signaled')):
            self.assertFalse(control_lane.recover(self.db, scope_key='scope', current_owner='new'))
        self.assertIsNone(process.poll())
        self.assertEqual(self.state(), 'running')

    def test_legacy_unknown_can_be_inspected_and_explicitly_acknowledged(self):
        self.child(context=None)
        with mock.patch.object(worker_identity, 'terminate_recorded_identity', return_value={'status': 'attention'}):
            self.assertFalse(control_lane.recover(self.db, scope_key='scope', current_owner='new'))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(control_lane.main(['--db', str(self.db), '--list']), 0)
        inspection = json.loads(output.getvalue())
        self.assertEqual(inspection['children'][0]['child_id'], 'child')
        self.assertFalse(inspection['children'][0]['owner_lease_active'])
        output = io.StringIO()
        with redirect_stdout(output), \
             mock.patch.object(worker_identity, 'terminate_recorded_identity', side_effect=AssertionError('acknowledgment must not signal')):
            self.assertEqual(control_lane.main(['--db', str(self.db), '--acknowledge-stopped', '--id', 'child']), 0)
            self.assertTrue(control_lane.recover(self.db, scope_key='scope', current_owner='new'))
        self.assertEqual(self.state(), 'stopped')
        with closing(sqlite3.connect(self.db)) as conn:
            evidence = json.loads(conn.execute("SELECT metadata_json FROM daemon_events WHERE event_type='control_child_retired'").fetchone()[0])
        self.assertEqual(evidence['resolution'], 'operator_acknowledged_stopped')
        self.assertEqual(evidence['child_id'], 'child')
        self.assertEqual(evidence['acknowledged_by_uid'], os.getuid())

    def test_acknowledgment_refuses_live_owner_without_clearing_record(self):
        self.child(active=True)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertNotEqual(control_lane.main(['--db', str(self.db), '--acknowledge-stopped', '--id', 'child']), 0)
        self.assertEqual(self.state(), 'running')
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM daemon_events').fetchone()[0], 0)

    def test_boot_context_probe_is_bounded_and_unknown_probe_does_not_prove_reboot(self):
        with mock.patch.object(control_lane.sys, 'platform', 'darwin'), \
             mock.patch.object(control_lane.subprocess, 'run', side_effect=subprocess.TimeoutExpired('sysctl', 2)) as command:
            self.assertIsNone(control_lane._boot_context())
        self.assertEqual(command.call_args.kwargs['timeout'], 2)

    def test_existing_child_wrapper_records_boot_context_and_preserves_json_output(self):
        import time
        lane = control_lane.ControlLane(self.db, 'scan', 'owner')
        self.addCleanup(lane.stop)
        lane.start([sys.executable, '-c', "print('{\"ok\":true,\"status\":\"done\"}')"], timeout_seconds=10)
        deadline = time.monotonic() + 5
        result = None
        while time.monotonic() < deadline and result is None:
            result = lane.poll()
            if result is None:
                time.sleep(.01)
        self.assertIsNotNone(result)
        self.assertTrue(result['ok'], result)
        with closing(sqlite3.connect(self.db)) as conn:
            recorded = json.loads(conn.execute('SELECT identity_json FROM github_control_children').fetchone()[0])
        if sys.platform.startswith('linux') or sys.platform == 'darwin':
            self.assertIsNotNone(recorded.get('execution_context'))
        self.assertEqual(result['status'], 'done')

    def test_macos_boot_probe_uses_boot_session_uuid(self):
        token = 'a1111111-2222-4333-8444-555555555555'
        response = subprocess.CompletedProcess([], 0, token.upper() + '\n', '')
        with mock.patch.object(control_lane.sys, 'platform', 'darwin'), \
             mock.patch.object(control_lane.subprocess, 'run', return_value=response) as command:
            context = control_lane._boot_context()
        self.assertEqual(context['boot_id'], token)
        self.assertEqual(context['boot_source'], 'macos_boot_session_uuid')
        self.assertEqual(command.call_args.args[0], ['sysctl', '-n', 'kern.bootsessionuuid'])
        self.assertFalse(control_lane._different_boot({'execution_context': OLD_BOOT},
                         {**NEW_BOOT, 'boot_source': 'unknown_boot_format'}))
