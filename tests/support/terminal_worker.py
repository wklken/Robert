"""Controlled worker used by terminal monitor and fixture startup tests."""

from subprocess import Popen
import sys
import time

from tests.support.temp_workspace import TemporaryWorkspace
from robert_agent.adapters.processes import identity


class TerminalWorkerFixture(TemporaryWorkspace):
    def start_worker(self):
        ready = self.root / "worker.ready"
        code = f"import time; from pathlib import Path; Path({str(ready)!r}).touch(); time.sleep(120)"
        self.worker = Popen([sys.executable, "-c", code], start_new_session=True)
        self.addCleanup(self.stop_worker)
        deadline = time.monotonic() + 5
        while not ready.exists() and self.worker.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists(), "worker did not become ready within 5 seconds")
        self.dispatch = identity.capture_process_identity(self.worker.pid)

    def stop_worker(self):
        if self.worker.poll() is None:
            self.worker.kill()
        self.worker.wait(timeout=3)
