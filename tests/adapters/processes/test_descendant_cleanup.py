"""Only owned temporary processes are started by these cleanup regressions."""

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from robert_agent.adapters.processes import identity


class DescendantCleanupTests(unittest.TestCase):
    def test_term_exit_of_leader_does_not_leave_verified_term_resistant_child(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        ready = root / "child.json"
        child = (
            "import os,signal,time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(30)"
        )
        parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(30)"
        proc = subprocess.Popen([sys.executable, "-c", parent], start_new_session=True)
        self.addCleanup(identity.cleanup_new_process, proc, wait_timeout=0.2)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists())
        recorded = identity.capture_process_identity(proc.pid)
        child_identity = identity.capture_process_identity(int(ready.read_text()))
        result = identity.terminate_recorded_identity(recorded, timeout_seconds=0.2)
        proc.wait(timeout=3)
        self.assertEqual(result["status"], "terminated")
        self.assertFalse(identity.live_process_group(proc.pid), result)
        self.assertEqual(identity.inspect_recorded_identity(child_identity), "dead")
