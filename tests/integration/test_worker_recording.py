from contextlib import closing
import sqlite3
from tests.support.worker_protocol import WorkerProtocolFixture


class WorkerRecordingTests(WorkerProtocolFixture):
    def test_snapshot_and_result_write_sqlite_rows(self):
        from robert_agent.delivery.application import submission as result
        from robert_agent.execution import progress as snapshot

        db_path = self._init_attempt_db()

        snapshot_result = snapshot.record_snapshot(
            db_path=db_path,
            task_id="task-1",
            attempt_id="attempt-1",
            phase="verify",
            status="running",
            summary="Running focused tests",
            next_step="Record result",
        )
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[{"type": "comment", "body": "Completed analysis."}],
            consumed_event_fingerprints=["comment:1"],
            verification=[{"command": "python -m unittest", "status": "passed", "purpose": "regression", "required": False, "exit_code": 0, "source": "worker_reported"}],
            handoff="done",
            used_skills=["fast-code-path"],
        )
        result_result = result.record_result(db_path, payload)

        self.assertTrue(snapshot_result["ok"], snapshot_result)
        self.assertTrue(result_result["ok"], result_result)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            phase_count = conn.execute("SELECT COUNT(*) FROM worker_phases").fetchone()[0]
            result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
            action_count = conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0]
        self.assertEqual(phase_count, 1)
        self.assertEqual(result_count, 1)
        self.assertEqual(action_count, 1)
