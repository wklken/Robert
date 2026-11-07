"""Shared fixtures for terminal github followup tests."""

from robert_agent.work.application import synchronization as synchronization
from robert_agent.work import scan_batches as scan_batches
from contextlib import closing
import json
import sqlite3
import unittest
from tests.support import board_control_integration as fixtures
from tests.support import PACKAGE_ROOT
from robert_agent.persistence import execution_control as execution_store
from robert_agent.runtime import cycle as run_once
from robert_agent.foundation import values


class TerminalGithubFollowupFixture(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BoardControlIntegrationFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db_path = self.fixture.db_path
        self.source_type = "issue"

    def deliver(self, comment_id, *, actor="owner", body="@robot analyze caching", intent="analysis"):
        path = self.fixture.root / "comments.json"
        path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": comment_id,
                            "number": 123,
                            "source_type": self.source_type,
                            "event_type": "comment",
                            "actor_login": actor,
                            "body": body,
                            "intent": intent,
                            "state": "open",
                        }
                    ]
                }
            )
        )
        result = run_once.run_once(
            self.fixture.config_path,
            workflow_path=PACKAGE_ROOT / "resources/workflow.yml",
            fixture_path=path,
            dry_run=True,
            skip_external=True,
            skip_publish=True,
        )
        self.assertTrue(result["ok"], result)
        return result

    def finish(self, lifecycle="completed"):
        self.deliver("first")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self.parent_id, self.stream_id = conn.execute("SELECT task_id,workstream_id FROM tasks").fetchone()
            conn.execute("UPDATE tasks SET lifecycle=?", (lifecycle,))
            conn.execute(
                "UPDATE attempts SET status=?,finished_at=?,metadata_json=?",
                (lifecycle, values.utc_now(), '{"result_contract":"compact-v1"}'),
            )
            conn.execute("UPDATE workstreams SET lifecycle=?,active_task_id=NULL", (lifecycle,))
            execution_store.close_binding(conn, "task", self.parent_id, values.utc_now())
            if lifecycle == "completed":
                conn.execute("UPDATE work_items SET completed_at=?", (values.utc_now(),))
            self.old_binding = execution_store.get_binding(conn, "task", self.parent_id)

    def tasks(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(
                "SELECT task_id,parent_task_id,lifecycle FROM tasks ORDER BY created_at,task_id"
            ).fetchall()
