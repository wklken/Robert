"""Regression contracts for bounded recovery and durable user outcomes."""

from contextlib import closing


import sqlite3


from unittest import mock

from tests.support import PACKAGE_ROOT  # noqa: F401
from tests.support.durable_result import DurableResultFixture


class DurableResultTests(DurableResultFixture):
    def test_locked_final_result_replays_once_without_rerunning_worker(self):
        from robert_agent.delivery.application import submission as result

        with closing(sqlite3.connect(self.db)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            response = result.record_result(self.db, self.payload)
            self.assertTrue(response["ok"])
            self.assertEqual(response["status"], "deferred")
            blocker.rollback()
        result.replay_pending_results(self.db)
        result.replay_pending_results(self.db)
        duplicate = result.record_result(self.db, self.payload)
        self.assertTrue(duplicate["ok"])
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(
                c.execute("SELECT count(*) FROM worker_results").fetchone()[0], 1
            )
            self.assertEqual(c.execute("SELECT count(*) FROM wakeups").fetchone()[0], 1)

    def test_canceled_attempt_cannot_be_revived_by_result_replay(self):
        from robert_agent.delivery.application import submission as result

        with mock.patch.object(
            result,
            "_record_result_db",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            result.record_result(self.db, self.payload)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE attempts SET status='canceled'")
            c.execute("UPDATE tasks SET lifecycle='canceled'")
        replay = result.replay_pending_results(self.db)
        self.assertEqual(replay["rejected"], 1)
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(
                c.execute("SELECT count(*) FROM worker_results").fetchone()[0], 0
            )
