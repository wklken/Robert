"""Regression contracts for bounded recovery and durable user outcomes."""

from tests.support.durable_result import DurableResultFixture


from contextlib import closing


import sqlite3


from tests.support import PACKAGE_ROOT  # noqa: F401


class WaitingResultTests(DurableResultFixture):
    def test_github_question_without_public_action_is_rejected_before_completion(self):
        from robert_agent.delivery.application import submission as result

        self.payload["output_type"] = "waiting_for_user"
        self.payload["submission"].update(
            operator_question={
                "kind": "clarification",
                "summary": "Which version?",
                "choices": [],
            },
        )
        response = result.record_result(self.db, self.payload)
        self.assertEqual(response["status"], "failed_validation")
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(
                c.execute("SELECT lifecycle FROM tasks").fetchone()[0], "running"
            )
            self.assertEqual(
                c.execute("SELECT status FROM attempts").fetchone()[0], "running"
            )
            self.assertIsNone(
                c.execute("SELECT failure_json FROM attempts").fetchone()[0]
            )
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(
                c.execute("SELECT count(*) FROM worker_results").fetchone()[0], 0
            )
            self.assertEqual(
                c.execute("SELECT count(*) FROM notifications").fetchone()[0], 0
            )
        self.payload["output_type"] = "local_result"
        self.payload["submission"].pop("operator_question")
        self.assertTrue(result.record_result(self.db, self.payload)["ok"])
