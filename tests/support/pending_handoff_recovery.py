"""Shared fixtures for pending handoff recovery tests."""

from contextlib import closing
import json
import sqlite3
import unittest
from tests.support import repository_cycle as fixtures
from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.runtime import cycle as run_once
from robert_agent.configuration import command as validate_config
from robert_agent.delivery.application import submission as worker_result


class PendingHandoffRecoveryFixture(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RepositoryCycleFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.data_dir / "dd.sqlite3"
        self.cycle(self.fixture.fixture_path)
        with closing(sqlite3.connect(self.db)) as conn:
            self.task, self.attempt = conn.execute("SELECT task_id,attempt_id FROM attempts").fetchone()
        self.fixture.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "pending",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot implement the second change",
                            "intent": "bug_fix",
                            "state": "open",
                        }
                    ]
                }
            )
        )
        self.cycle(self.fixture.fixture_path)
        receipt = worker_result.record_result(
            self.db,
            {
                "task_id": self.task,
                "attempt_id": self.attempt,
                "output_type": "new_pr",
                "submission": {
                    "planned_github_actions": self.fixture._new_pr_actions_for_attempt(self.db, self.task),
                    "consumed_event_fingerprints": ["comment:comment-1"],
                    "verification": [fixtures._verification_evidence()],
                    "handoff": "done",
                    "used_skills": ["fast-small-pr"],
                },
            },
        )
        self.assertTrue(receipt["ok"], receipt)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE github_actions SET publish_status='published'")
        self.empty = self.fixture.root / "empty.json"
        self.empty.write_text('{"events":[]}')
        self.config = validate_config.validate_config(self.fixture.config_path, skip_external=True)

    def cycle(self, fixture=None):
        return run_once.run_once(
            self.fixture.config_path,
            fixture_path=fixture,
            dry_run=True,
            skip_external=True,
            skip_publish=True,
            consume_observations=True,
        )

    def state(self):
        with closing(sqlite3.connect(self.db)) as conn:
            task = conn.execute(
                "SELECT lifecycle,metadata_json FROM tasks WHERE task_id=?",
                (self.task,),
            ).fetchone()
            return task[0], json.loads(task[1]).get("pending_handoff", {})
