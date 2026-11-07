"""Shared fixtures for review input workflow tests."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import unittest
from tests.support import PACKAGE_ROOT  # noqa: F401
from tests.support import repository_cycle as run_tests


class ReviewInputIntegrationFixture(unittest.TestCase):
    setUp = run_tests.RepositoryCycleFixture.setUp

    def prepare(self, event_type):
        from robert_agent.runtime import cycle as run_once

        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "request-1",
                            "number": 456,
                            "source_type": "pull_request",
                            "event_type": event_type,
                            "actor_login": "wklken",
                            "body": "@robert-bot add a null check",
                            "intent": "small_change",
                            "has_open_dd_pr": True,
                            "existing_pr_head_branch": "codex/fix",
                            "existing_pr_head_sha": "base-sha",
                            "pr_author_login": "robert-bot",
                            "url": "https://github.com/example/backend/pull/456",
                        }
                    ]
                }
            )
        )
        run = run_once.run_once(
            self.config_path,
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(run["ok"], run)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn:
            task_id, attempt_id, metadata = conn.execute(
                "SELECT t.task_id,a.attempt_id,t.metadata_json FROM tasks t JOIN attempts a ON a.task_id=t.task_id"
            ).fetchone()
            prompt_path = conn.execute(
                "SELECT path FROM artifacts WHERE task_id=? AND artifact_type='prompt'",
                (task_id,),
            ).fetchone()[0]
        return (
            db_path,
            task_id,
            attempt_id,
            json.loads(metadata),
            Path(prompt_path).read_text(),
        )
