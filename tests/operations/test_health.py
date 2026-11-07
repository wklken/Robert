"""Regression contracts for bounded recovery and durable user outcomes."""

from tests.support.publication_budget import PublicationBudgetFixture


from contextlib import closing


import sqlite3


from tests.support import PACKAGE_ROOT  # noqa: F401


class HealthTests(PublicationBudgetFixture):
    def test_health_reports_delayed_publication_and_storage_without_deleting(self):
        from robert_agent.operations import health as reliability_health

        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute(
                "UPDATE github_actions SET created_at='2020-01-01T00:00:00+00:00'"
            )
        first = reliability_health.collect(
            self.db, now=self.now.isoformat(), repo_ids=["repo"]
        )
        reliability_health.collect(self.db, now=self.now.isoformat(), repo_ids=["repo"])
        self.assertEqual(first["delayed_tasks_sample"], 1)
        self.assertGreater(first["database_bytes"], 0)
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(
                c.execute("SELECT count(*) FROM notifications").fetchone()[0], 1
            )
            self.assertEqual(c.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
