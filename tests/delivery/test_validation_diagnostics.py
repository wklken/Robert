from contextlib import closing
import json
import sqlite3

from robert_agent.delivery.application.validation import record_diagnostic
from tests.support.worker_protocol import WorkerProtocolFixture


class ValidationDiagnosticTests(WorkerProtocolFixture):
    def test_rejection_count_is_scoped_to_attempt_without_using_the_result_slot(self):
        db = self._init_attempt_db()
        envelope = {
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "handoff": "private submission text",
        }

        record_diagnostic(db, envelope, "invalid_action")
        record_diagnostic(db, envelope, "invalid_action")
        record_diagnostic(db, {**envelope, "task_id": "wrong-task"}, "invalid_action")

        with closing(sqlite3.connect(db)) as conn:
            status, metadata = conn.execute("SELECT status, metadata_json FROM attempts").fetchone()
            self.assertEqual(status, "running")
            self.assertEqual(
                json.loads(metadata)["submission_validation_counts"],
                {"invalid_action": 2},
            )
            self.assertNotIn(envelope["handoff"], metadata)
            self.assertEqual(conn.execute("SELECT result_id FROM worker_results").fetchall(), [])

    def test_locked_diagnostics_sink_does_not_change_submission_state(self):
        db = self._init_attempt_db()
        with closing(sqlite3.connect(db)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            record_diagnostic(db, {"task_id": "task-1", "attempt_id": "attempt-1"}, "invalid_action")
            blocker.rollback()

        with closing(sqlite3.connect(db)) as conn:
            status, metadata = conn.execute("SELECT status, metadata_json FROM attempts").fetchone()
            self.assertEqual(status, "running")
            self.assertNotIn("submission_validation_counts", json.loads(metadata))
