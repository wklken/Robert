import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import run_once


class ProcessTerminationTests(unittest.TestCase):
    def test_timeout_terminates_descendants_even_after_leader_exits(self):
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
                        self.assertEqual(run_once._dispatch_process_status(
                            {"dispatch": {"pid": proc.pid}})["status"], "alive")
                    run_once._terminate_attempt_process({'dispatch': {'pid': proc.pid}})
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
                run_once._terminate_attempt_process({'dispatch': {'pid': proc.pid}})
                self.assertIsNotNone(proc.poll())
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                proc.wait(timeout=5)
