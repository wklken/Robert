"""Regression contracts for bounded recovery and durable user outcomes."""

from contextlib import closing


import sqlite3


import unittest


from tests.support import PACKAGE_ROOT  # noqa: F401


from robert_agent.adapters.sqlite import execution_control as execution_store


class RecoveryEntryTests(unittest.TestCase):
    def test_operator_can_start_new_subject_after_attention_and_dead_worker(self):
        from tests.support.work_items import WorkItemCommandFixture
        from robert_agent.work.application import commands as work_items
        from robert_agent.execution import ownership as execution_ownership

        fixture = WorkItemCommandFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        task_id = fixture._failed_github_task()
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            conn.execute(
                "UPDATE tasks SET lifecycle='running' WHERE task_id=?", (task_id,)
            )
            execution_store.ensure_subject(
                conn, "task", task_id, "human_request", "2026-10-24T00:00:00+00:00"
            )
            execution_store.mark_binding_attention(
                conn, "task", task_id, "2026-10-24T00:00:00+00:00"
            )
            conn.execute(
                "UPDATE tasks SET lifecycle='failed' WHERE task_id=?", (task_id,)
            )
        response = work_items.retrigger_failed_github_task(
            fixture.db_path,
            task_id,
            context=fixture.context,
            idempotency_key="recover-auth",
        )
        with closing(sqlite3.connect(fixture.db_path)) as conn:
            self.assertEqual(
                execution_store.get_binding(conn, "task", task_id)["binding_status"],
                "terminal",
            )
            self.assertTrue(
                execution_ownership.parent_allows_child(
                    conn, task_id, retrigger_wakeup_id=response["wakeup_id"]
                )
            )
