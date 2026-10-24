import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import run_once, worker_identity


class ProcessTerminationTests(unittest.TestCase):
    def test_timeout_terminates_verified_group_and_refuses_unverified_orphans(self):
        for leader_exits in (False, True):
            with self.subTest(leader_exits=leader_exits), tempfile.TemporaryDirectory() as tmp:
                ready = Path(tmp) / 'ready'
                child = "import time; time.sleep(30)"
                parent = (
                    "import subprocess,sys,time; from pathlib import Path; "
                    f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
                    f"Path({str(ready)!r}).write_text(str(p.pid)); "
                    + ("" if leader_exits else "time.sleep(30)")
                )
                proc = subprocess.Popen([sys.executable, '-c', parent], start_new_session=True)
                try:
                    deadline = time.monotonic() + 5
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(ready.exists())
                    child_pid = int(ready.read_text())
                    if leader_exits:
                        proc.wait(timeout=5)
                        outcome = run_once._terminate_attempt_process({'dispatch': {'pid': proc.pid}})
                        self.assertEqual(outcome['status'], 'attention')
                        self.assertNotIn(run_once._process_state(child_pid), (None, 'Z', 'X', 'x'))
                        continue
                    identity = worker_identity.capture_process_identity(proc.pid)
                    outcome = run_once._terminate_attempt_process({'dispatch': identity})
                    self.assertEqual(outcome['status'], 'terminated', outcome)
                    deadline = time.monotonic() + 1
                    while run_once._process_state(child_pid) not in (None, 'Z', 'X', 'x') and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertIn(run_once._process_state(child_pid), (None, 'Z', 'X', 'x'))
                finally:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    proc.wait(timeout=5)

    def test_dead_leader_with_live_group_does_not_allow_automatic_resume(self):
        from unittest.mock import patch
        with patch.object(worker_identity, 'inspect_recorded_identity', return_value='dead'), \
             patch.object(run_once, '_live_process_group', return_value=True):
            result = run_once._dispatch_process_status({'dispatch': {'pid': 1234}})
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['reason'], 'unverified_descendants_running')

    def test_timeout_escalates_when_worker_ignores_sigterm(self):
        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp) / 'ready'
            code = ("import signal,time; from pathlib import Path; "
                    "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                    f"Path({str(ready)!r}).touch(); time.sleep(30)")
            proc = subprocess.Popen([sys.executable, '-c', code], start_new_session=True)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists())
                identity = worker_identity.capture_process_identity(proc.pid)
                outcome = run_once._terminate_attempt_process({'dispatch': identity})
                self.assertIsNotNone(proc.poll(), outcome)
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                proc.wait(timeout=5)
