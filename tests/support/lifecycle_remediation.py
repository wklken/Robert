"""Shared fixtures for lifecycle remediation tests."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import subprocess
import sys
import time
import unittest
from tests.support import durable_result as result_fixture
from robert_agent.adapters.processes import worker as dispatch
from robert_agent.adapters.sqlite import execution_control as execution_store
from robert_agent.adapters.processes import identity as worker_identity


class LifecycleRemediationFixture(unittest.TestCase):
    def process(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            start_new_session=True,
        )
        self.addCleanup(self.stop_process, process)
        time.sleep(0.1)
        return process, worker_identity.capture_process_identity(process.pid)

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        dispatch._BACKGROUND_PROCESSES.pop(process.pid, None)

    def running_fixture(self, *, age_minutes=0):
        fixture = result_fixture.DurableResultFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        process, identity = self.process()
        now = datetime.now(timezone.utc)
        old = (now - timedelta(minutes=age_minutes)).isoformat()
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET status='running',started_at=?,heartbeat_at=?,metadata_json=json_set(?, '$.result_contract', 'compact-v1')",
                (old, old, json.dumps({"dispatch": identity})),
            )
            task, repo = conn.execute("""SELECT t.task_id,w.repo_id FROM tasks t
                JOIN workstreams w ON w.workstream_id=t.workstream_id""").fetchone()
            execution_store.ensure_subject(
                conn, "task", task, "human_request", now.isoformat()
            )
            conn.execute(
                """INSERT INTO worker_phases(
                phase_id,attempt_id,phase,status,summary,created_at)
                VALUES ('started','attempt-1','analyze','running','Analyzing',?)""",
                (old,),
            )
        return fixture, process, repo, now.isoformat()
