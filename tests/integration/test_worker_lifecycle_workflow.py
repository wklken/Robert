from contextlib import closing
import sqlite3
import unittest
from tests.support.worker_lifecycle_workflow import WorkerLifecycleWorkflowFixture


class WorkerLifecycleWorkflowTests(WorkerLifecycleWorkflowFixture):
    def test_dead_recoverable_progress_consumes_quota_once_then_attention(self):
        from robert_agent.execution import operations as execution_operations
        from robert_agent.persistence import execution_control as execution_store

        db_path = self._database()
        evidence = {
            "identity_status": "dead",
            "status": "recoverable_progress",
            "task_id": "task:test",
            "previous_attempt_id": "attempt:test",
            "progress_signals": ["dirty_worktree"],
        }
        now = "2026-08-02T00:01:00+00:00"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            self.assertEqual(
                execution_operations.classify_recovery(conn, "task", "task:test", "attempt:test", evidence, now=now),
                "recover_once",
            )
            self.assertEqual(
                execution_store.get_binding(conn, "task", "task:test")["recovery_attempts"],
                1,
            )
            self.assertEqual(
                execution_operations.classify_recovery(conn, "task", "task:test", "attempt:test", evidence, now=now),
                "attention",
            )
            self.assertEqual(
                execution_store.get_binding(conn, "task", "task:test")["binding_status"],
                "attention",
            )

    def test_alive_same_never_consumes_recovery_quota(self):
        from robert_agent.execution import operations as execution_operations
        from robert_agent.persistence import execution_control as execution_store

        db_path = self._database()
        evidence = {
            "identity_status": "alive_same",
            "status": "recoverable_progress",
            "task_id": "task:test",
            "previous_attempt_id": "attempt:test",
            "progress_signals": ["dirty_worktree"],
        }
        with closing(sqlite3.connect(db_path)) as conn, conn:
            self.assertEqual(
                execution_operations.classify_recovery(
                    conn,
                    "task",
                    "task:test",
                    "attempt:test",
                    evidence,
                    now="2026-08-02T00:01:00+00:00",
                ),
                "continue",
            )
            self.assertEqual(
                execution_store.get_binding(conn, "task", "task:test")["recovery_attempts"],
                0,
            )


if __name__ == "__main__":
    unittest.main()
