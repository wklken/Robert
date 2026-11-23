"""Regression contracts for bounded recovery and durable user outcomes."""

from tests.support.publication_budget import PublicationBudgetFixture


from contextlib import closing


import sqlite3


from tests.support import PACKAGE_ROOT  # noqa: F401


class HealthTests(PublicationBudgetFixture):
    def test_health_reports_delayed_publication_and_storage_without_deleting(self):
        from robert_agent.operations import health as reliability_health

        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE github_actions SET created_at='2020-01-01T00:00:00+00:00'")
        first = reliability_health.collect(self.db, now=self.now.isoformat(), repo_ids=["repo"])
        reliability_health.collect(self.db, now=self.now.isoformat(), repo_ids=["repo"])
        self.assertEqual(first["delayed_tasks_sample"], 1)
        self.assertGreater(first["database_bytes"], 0)
        self.assertNotIn("pending_outbox", first)
        self.assertNotIn("oldest_outbox_seconds", first)
        self.assertNotIn("attention_outbox", first)
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM notifications").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)

    def test_delay_notice_explains_the_specific_wait(self):
        import json

        from robert_agent.operations import health as reliability_health
        from robert_agent.delivery.messages import envelope

        for reason in ("queue_delayed", "publication_delayed"):
            with self.subTest(reason=reason):
                with closing(sqlite3.connect(self.db)) as c, c:
                    c.execute("DELETE FROM notifications")
                    c.execute("UPDATE tasks SET lifecycle='queued',created_at='2020-01-01T00:00:00+00:00'")
                    c.execute(
                        "UPDATE github_actions SET created_at=?",
                        ("2020-01-01T00:00:00+00:00" if reason == "publication_delayed" else self.now.isoformat(),),
                    )
                reliability_health.collect(self.db, now=self.now.isoformat(), repo_ids=["repo"])
                with closing(sqlite3.connect(self.db)) as c:
                    identifier, raw = c.execute("SELECT notification_id,metadata_json FROM notifications").fetchone()
                    payload = envelope(c, identifier)
                self.assertEqual(json.loads(raw)["reason_code"], reason)
                self.assertNotIn("新通知", payload["summary"])
                self.assertNotIn("Inspect queue capacity", payload["required_action"])
                self.assertIn("排队" if reason == "queue_delayed" else "发布", payload["summary"])
