from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.adapters.processes import identity as worker_identity
from tests.support.supervision_repair_worker import (
    BrokerFixture,
    SUBMISSION,
    broker_result,
    stop_fixture,
    submission_source,
)


def wait_for_file(path):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size:
            return path.read_text()
        time.sleep(0.01)
    raise AssertionError(f"Fixture did not reach {path.name}")


class BrokerProcessTests(unittest.TestCase):
    def fixture(self, source, *, timeout=5):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        fixture = BrokerFixture(Path(temp.name), source, timeout=timeout)
        fixture.bind()
        return fixture

    def launch(self, fixture, **options):
        process = fixture.launch(**options)
        self.addCleanup(stop_fixture, process)
        return process

    def test_trusted_broker_ignores_repository_python_package(self):
        from robert_agent.adapters.processes import worker as dispatch
        from robert_agent.workflows.maintenance import (
            worker as supervision_repair_worker,
        )

        fixture = self.fixture('raise AssertionError("model must not start")')
        package = fixture.tree / "robert_agent"
        package.mkdir()
        marker = fixture.root / "host-import-marker"
        (package / "__init__.py").write_text(f"from pathlib import Path; Path({str(marker)!r}).touch()")
        prompt = fixture.root / "prompt.txt"
        prompt.write_text("fixture")
        profile = supervision_repair_worker.launch_profile(
            fixture.config, fixture.task, fixture.attempt, fixture.config["workers"][0]
        )
        launched = dispatch.dispatch_worker(
            fixture.task,
            fixture.attempt,
            prompt,
            worktree_path=fixture.tree,
            worker_agent_config=profile,
            dry_run=False,
            startup_probe_seconds=0.5,
        )
        self.assertFalse(marker.exists(), launched)
        self.assertEqual(launched.get("returncode"), 1, launched)
        stderr = Path(launched["stderr_path"]).read_text()
        self.assertNotIn("No module named robert_agent.workflows.maintenance.worker", stderr)

    def test_valid_output_uses_existing_result_submission(self):
        fixture = self.fixture(
            "import json,sys\nrequest = json.load(sys.stdin)\n"
            'assert request["result_transport"] == "stdout_compact_submission"\n' + submission_source()
        )
        report = broker_result(self.launch(fixture))
        self.assertTrue(report["ok"], report)
        self.assertEqual(fixture.counts(), (1, 2))
        with closing(sqlite3.connect(fixture.db)) as conn:
            row = conn.execute("SELECT task_id,attempt_id,output_type FROM worker_results").fetchone()
            self.assertEqual(row, (fixture.task, fixture.attempt, "new_pr"))

    def test_invalid_and_oversized_output_cannot_reserve_a_result(self):
        for source, expected in (
            ('print("[]")', "invalid_repair_submission"),
            ('print("x" * (130 * 1024))', "repair_result_too_large"),
            (
                f"print({json.dumps({**SUBMISSION, 'task_id': 'forged'})!r})",
                "failed_validation",
            ),
        ):
            with self.subTest(expected=expected):
                fixture = self.fixture(source)
                self.assertEqual(broker_result(self.launch(fixture))["status"], expected)
                self.assertEqual(fixture.counts(), (0, 0))

    def sleepy(self, timeout):
        source = """
import pathlib, subprocess, sys, time
child = "import os,pathlib,signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
subprocess.Popen([sys.executable, '-c', child, str(pathlib.Path(__file__).parent / 'inner-child')])
time.sleep(30)
"""
        return self.fixture(source, timeout=timeout)

    def test_timeout_and_cancellation_reap_term_resistant_grandchild(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                fixture = self.sleepy(timeout=8 if cancel else 1)
                process = self.launch(fixture)
                child_pid = int(wait_for_file(fixture.root / "inner-child"))
                self.assertEqual(os.getpgid(child_pid), process.pid)
                if cancel:
                    process.terminate()
                report = broker_result(process)
                self.assertEqual(
                    report["status"],
                    "repair_worker_stopped" if cancel else "repair_worker_timeout",
                )
                self.assertFalse(worker_identity.live_process_group(process.pid))
                self.assertEqual(fixture.counts(), (0, 0))

    def test_revoked_policy_after_launch_cannot_submit(self):
        fixture = self.fixture(
            """
import pathlib,time
root = pathlib.Path(__file__).parent
(root / 'ready').write_text('yes')
while not (root / 'finish').exists():
    time.sleep(.01)
"""
            + f"print({json.dumps(SUBMISSION)!r})\n"
        )
        process = self.launch(fixture)
        wait_for_file(fixture.root / "ready")
        fixture.config["supervision"]["repair"]["allowed_actions"] = ["open_pr"]
        fixture.save_config()
        (fixture.root / "finish").touch()
        report = broker_result(process)
        self.assertEqual(report["status"], "repair_policy_rejected")
        self.assertEqual(fixture.counts(), (0, 0))

    def test_broker_requires_its_own_process_group(self):
        fixture = self.fixture('raise AssertionError("must not launch")')
        report = broker_result(self.launch(fixture, private_group=False))
        self.assertEqual(report["status"], "repair_broker_group_not_isolated")
        self.assertEqual(fixture.counts(), (0, 0))
