"""Shared fixtures for reconcile isolation tests."""

from contextlib import closing
import json
import sqlite3
import subprocess
import unittest
from tests import support  # noqa: F401
from tests.support import repository_cycle as fixtures
from tests.support import retention as health_fixtures
from robert_agent.execution import runtime_state as daemon_state
from robert_agent.adapters.sqlite import execution_control as execution_store
from robert_agent.runtime import cycle as run_once


class ReconcileIsolationFixture(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RepositoryCycleFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.data_dir / "dd.sqlite3"

    def seed(self, status="terminal"):
        result = run_once.run_once(
            self.fixture.config_path,
            fixture_path=self.fixture.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            task_id = conn.execute("SELECT task_id FROM tasks").fetchone()[0]
            conn.execute(
                "UPDATE github_sources SET source_key='github:example/backend!123',source_type='pull_request',state='open'"
            )
            conn.execute("UPDATE tasks SET lifecycle='completed'")
            conn.execute("UPDATE attempts SET status='completed',metadata_json='{}'")
            conn.execute("UPDATE workstreams SET lifecycle='completed',active_task_id=NULL")
            execution_store.close_binding(conn, "task", task_id, "2026-10-28T07:00:00Z")
            conn.execute("UPDATE execution_subjects SET binding_status=?", (status,))
        return task_id

    @staticmethod
    def remote(args, **kwargs):
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps({"state": "closed", "merged": True, "merged_at": "2026-10-28T07:10:33Z"}),
            "",
        )


class ControlFailureHealthFixture(unittest.TestCase):
    def setUp(self):
        f = health_fixtures.RetentionFixture()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.db = f.db
        self.now = "2026-10-28T12:00:00+00:00"

    def event(self, event_type, status, minute):
        with closing(daemon_state.connect(self.db)) as conn, conn:
            daemon_state.record_event(
                conn,
                "daemon",
                event_type,
                status,
                now=f"2026-10-28T11:{minute:02d}:00+00:00",
            )
