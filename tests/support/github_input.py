"""Temporary repository-cycle fixtures for GitHub request authority tests."""

from contextlib import closing
import json
import sqlite3
from robert_agent.delivery.application import submission
from robert_agent.runtime import cycle
from tests.support.repository_cycle import RepositoryCycleFixture


class GitHubInputFixture(RepositoryCycleFixture):
    def db(self):
        return self.data_dir / "dd.sqlite3"

    def scan(self, events, *, consume_observations=False):
        self.fixture_path.write_text(json.dumps({"events": events}))
        value = cycle.run_once(
            self.config_path,
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
            skip_publish=True,
            consume_observations=consume_observations,
        )
        self.assertTrue(value["ok"], value)
        return value

    def rows(self, sql, args=()):
        with closing(sqlite3.connect(self.db())) as conn:
            return conn.execute(sql, args).fetchall()

    def event(self, id="trusted", actor="wklken", body="@robert-bot please classify this", **extra):
        return {
            "id": id,
            "number": 123,
            "source_type": "issue",
            "event_type": "comment",
            "actor_login": actor,
            "body": body,
            "intent": "unclear",
            "state": "open",
            "url": "https://github.com/example/backend/issues/123",
            **extra,
        }

    def finish(self, fingerprints, *, output_type="classification_result", **extra):
        task_id, attempt_id = self.rows("SELECT task_id,attempt_id FROM attempts ORDER BY rowid DESC LIMIT 1")[0]
        value = submission.record_result(
            self.db(),
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": output_type,
                "submission": {
                    "planned_github_actions": [],
                    "consumed_event_fingerprints": fingerprints,
                    "verification": [],
                    "used_skills": [],
                    "handoff": "Bounded offline classification result",
                    **extra,
                },
            },
        )
        self.assertTrue(value["ok"], value)
        result = self.scan([])
        return task_id, result
