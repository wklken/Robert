"""Regression contracts for bounded recovery and durable user outcomes."""

from contextlib import closing
from datetime import timedelta

import json


import sqlite3


from tests.support import PACKAGE_ROOT  # noqa: F401


from robert_agent.runtime import loop as loop_engine
from tests.support.publication_budget import PublicationBudgetFixture


class PublicationBudgetTests(PublicationBudgetFixture):
    def test_permanent_rejection_stops_remote_calls_and_scheduler(self):
        self.cycle()
        rejected_at = self.now
        # Cover repeated ticks and long-term inactivity through the original
        # 1000 * 600 second horizon, without rerunning identical cycles 1000 times.
        for elapsed in (0, 0, 600, 1200, 3600, 86400, 600000):
            with self.subTest(elapsed=elapsed):
                self.now = rejected_at + timedelta(seconds=elapsed)
                self.assertEqual(self.cycle()["failed_count"], 0)
                self.assertEqual(self.calls.count(["git", "push"]), 1)
                with closing(sqlite3.connect(self.db)) as c:
                    self.assertFalse(loop_engine.has_runnable_local_work(c, now=self.now.isoformat()))
                    self.assertEqual(
                        c.execute("SELECT status FROM publication_intents").fetchone()[0],
                        "attention",
                    )

    def test_transient_rejection_backs_off_and_exhausts_budget(self):
        self.error = "connection reset by peer"
        self.cycle()
        self.assertEqual(self.cycle()["pending_count"], 0)
        for _ in range(8):
            self.cycle(60)
        self.assertEqual(self.calls.count(["git", "push"]), 3)

    def test_blocked_push_does_not_publish_later_comment(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute(
                "INSERT INTO github_actions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "comment",
                    "r",
                    "t",
                    "comment",
                    "https://github.com/x/y/issues/1",
                    "",
                    "accepted",
                    "not_published",
                    "zzz",
                    json.dumps(
                        {"body": "Done <!-- robert-comment task_id=task-1 attempt_id=attempt-1 action_index=0 -->"}
                    ),
                ),
            )
        self.cycle()
        self.assertEqual(self.cycle(600)["pending_count"], 0)
