"""Regression contracts for bounded recovery and durable user outcomes."""

from contextlib import closing


import sqlite3


import unittest


from tests.support import PACKAGE_ROOT  # noqa: F401


from robert_agent.execution import operations as execution_operations

from robert_agent.adapters.sqlite import execution_control as execution_store


class RecoveryBudgetTests(unittest.TestCase):
    def test_authentication_failure_never_consumes_resume_quota(self):
        from tests.support.worker_lifecycle_workflow import (
            WorkerLifecycleWorkflowFixture,
        )

        fixture = WorkerLifecycleWorkflowFixture()
        self.addCleanup(fixture.doCleanups)
        db = fixture._database()
        evidence = {
            "identity_status": "dead",
            "status": "recoverable_progress",
            "task_id": "task:test",
            "previous_attempt_id": "attempt:test",
            "progress_signals": ["worker_stderr_tail_available"],
            "logs": {"worker_stderr": {"tail": "401 Unauthorized"}},
        }
        with closing(sqlite3.connect(db)) as c, c:
            self.assertEqual(
                execution_operations.classify_recovery(
                    c,
                    "task",
                    "task:test",
                    "attempt:test",
                    evidence,
                    now="2026-10-23T00:00:00+00:00",
                ),
                "attention",
            )
            self.assertEqual(
                execution_store.get_binding(c, "task", "task:test")[
                    "recovery_attempts"
                ],
                0,
            )
