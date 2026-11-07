from contextlib import closing
import sqlite3
from tests.support.worker_protocol import WorkerProtocolFixture


class ProgressTests(WorkerProtocolFixture):
    def test_snapshot_builds_phase_row(self):
        from robert_agent.execution import progress as snapshot

        row = snapshot.build_snapshot(
            task_id="task-1",
            attempt_id="attempt-1",
            phase="analyze",
            status="running",
            summary="Reading code",
            next_step="Plan fix",
        )

        self.assertEqual(row["phase"], "analyze")
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["task_id"], "task-1")

    def test_snapshot_rejects_task_attempt_mismatch(self):
        from robert_agent.execution import progress as snapshot

        db_path = self._init_attempt_db()

        with self.assertRaisesRegex(ValueError, "attempt_id does not belong to task_id"):
            snapshot.record_snapshot(
                db_path=db_path,
                task_id="task-2",
                attempt_id="attempt-1",
                phase="verify",
                status="running",
                summary="wrong task",
            )

        with closing(sqlite3.connect(db_path)) as conn, conn:
            phase_count = conn.execute("SELECT COUNT(*) FROM worker_phases").fetchone()[0]
        self.assertEqual(phase_count, 0)

    def test_duplicate_progress_only_refreshes_heartbeat(self):
        from robert_agent.execution import progress

        db = self._init_attempt_db()
        progress.record_snapshot(db, "task-1", "attempt-1", "verify", "running", "Checking behavior")
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE attempts SET heartbeat_at = '2000-01-01T00:00:00+00:00'")

        progress.record_snapshot(db, "task-1", "attempt-1", "verify", "running", "Checking behavior")

        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(
                conn.execute("SELECT phase, summary FROM worker_phases").fetchall(),
                [("verify", "Checking behavior")],
            )
            (heartbeat,) = conn.execute("SELECT heartbeat_at FROM attempts WHERE attempt_id = 'attempt-1'").fetchone()
        self.assertGreater(heartbeat, "2000-01-01T00:00:00+00:00")

    def test_invalid_phase_leaves_progress_and_heartbeat_untouched(self):
        from robert_agent.execution import progress

        db = self._init_attempt_db()
        with closing(sqlite3.connect(db)) as conn:
            (initial,) = conn.execute("SELECT heartbeat_at FROM attempts").fetchone()
        with self.assertRaisesRegex(ValueError, "invalid worker phase"):
            progress.record_snapshot(db, "task-1", "attempt-1", "untrusted-phase", "running", "Unknown")
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute("SELECT phase FROM worker_phases").fetchall(), [])
            self.assertEqual(conn.execute("SELECT heartbeat_at FROM attempts").fetchone(), (initial,))
