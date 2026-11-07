"""Shared fixtures for paused worker supervision tests."""

from robert_agent.adapters.processes import identity as worker_identity
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest import mock
from tests.support import terminal_github_followup as fixtures
from robert_agent.runtime import daemon
from robert_agent.execution import runtime_state as daemon_state
from robert_agent.configuration import command as validate_config


class PausedWorkerSupervisionFixture(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TerminalGithubFollowupFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.deliver("first")
        self.db = self.fixture.db_path
        self.config_path = self.fixture.fixture.config_path
        self.config = validate_config.validate_config(
            self.config_path, skip_external=True
        )
        self.now = datetime.now(timezone.utc)
        with closing(daemon_state.connect(self.db)) as conn, conn:
            owner = daemon_state.start_daemon_run(
                conn,
                config_path=str(self.config_path),
                owner_id="owner",
                now=self.now.isoformat(),
            )["daemon_run_id"]
        self.context = daemon.DaemonContext(
            str(self.config_path),
            None,
            self.db,
            self.fixture.fixture.repo_id,
            3,
            self.config["daemon"],
            owner,
            "lease",
            repo_ids=[self.fixture.fixture.repo_id],
            runtime_config=self.config,
        )
        self.context.next_live_poll_at = "2099-01-01T00:00:00+00:00"
        self.context.background_lanes = {
            name: SimpleNamespace(
                process=None, poll=mock.Mock(return_value=None), start=mock.Mock()
            )
            for name in ("scan", "publish")
        }
        for _ in range(3):
            daemon._record_control_outcome(
                self.context,
                "local",
                {"ok": False, "status": "child_failed"},
                now=self.now.isoformat(),
            )

    def process(self, age=120):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            start_new_session=True,
        )
        self.addCleanup(self.stop, proc)
        time.sleep(0.05)
        identity = worker_identity.capture_process_identity(proc.pid)
        metadata = {
            "result_contract": "compact-v1",
            "dispatch": {**identity, "worker_launch": {"timeout_seconds": 60}},
        }
        old = (self.now - timedelta(seconds=age)).isoformat()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET status='running',started_at=?,heartbeat_at=?,metadata_json=?",
                (old, old, json.dumps(metadata)),
            )
        return proc

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=3)
