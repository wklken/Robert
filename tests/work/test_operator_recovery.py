"""Regression contracts for bounded recovery and durable user outcomes."""

from contextlib import closing


import sqlite3


import unittest
from unittest import mock


from tests.support import PACKAGE_ROOT  # noqa: F401


from robert_agent.adapters.github import discovery as discover
from robert_agent.execution import rounds
from robert_agent.persistence import database, execution_control as execution_store, transactions
from robert_agent.work.application import commands as work_items
from robert_agent.work.tapd_authority import TicketIdentity
from tests.support.work_items import WorkItemCommandFixture


class RecoveryEntryTests(unittest.TestCase):
    def test_operator_can_start_new_subject_after_attention_and_dead_worker(self):
        from robert_agent.execution import ownership as execution_ownership

        fixture = WorkItemCommandFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        task_id = fixture._failed_github_task()
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle='running' WHERE task_id=?", (task_id,))
            execution_store.ensure_subject(conn, "task", task_id, "human_request", "2026-10-24T00:00:00+00:00")
            execution_store.mark_binding_attention(conn, "task", task_id, "2026-10-24T00:00:00+00:00")
            conn.execute("UPDATE tasks SET lifecycle='failed' WHERE task_id=?", (task_id,))
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
                execution_ownership.parent_allows_child(conn, task_id, retrigger_wakeup_id=response["wakeup_id"])
            )


class NativeRetriggerBoundaryTests(WorkItemCommandFixture):
    def _adopt_pr(self, task_id):
        ticket = TicketIdentity("tapd.example", "001", "bug", "003")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO sources(source_id,source_key,provider,host,project_key,source_type,external_id,title)"
                " VALUES(?,?,'tapd',?,?,?,?,'Private ticket')",
                (ticket.source_id, ticket.source_id, *ticket.key),
            )
        with closing(database.connect(self.db_path, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            item = work_items.ensure_tapd_work_item(
                conn, repo_id="repo-1", source_id=ticket.source_id, now="2026-10-24T00:00:00+00:00"
            )
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE workstreams SET origin_workstream_id=? "
                "WHERE workstream_id=(SELECT workstream_id FROM tasks WHERE task_id=?)",
                (item["workstream_id"], task_id),
            )

    def _retrigger(self, task_id, key, notifier=None):
        return work_items.retrigger_failed_github_task(
            self.db_path, task_id, context=self.context, idempotency_key=key, notifier=notifier
        )

    def _retry_state(self, task_id):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return (
                conn.execute("SELECT * FROM tasks WHERE task_id=? OR parent_task_id=?", (task_id, task_id)).fetchall(),
                conn.execute("SELECT * FROM wakeups WHERE task_id=?", (task_id,)).fetchall(),
                conn.execute(
                    "SELECT * FROM execution_subjects WHERE subject_type='task' AND subject_id=?", (task_id,)
                ).fetchall(),
            )

    def test_adopted_pr_refuses_retry_before_refresh_or_exit_observation(self):
        task_id = self._failed_github_task()
        self._adopt_pr(task_id)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE tasks SET metadata_json=? WHERE task_id=?",
                ('{"execution_attention":{"reason_code":"result_contract_cutover"}}', task_id),
            )
        before = self._retry_state(task_id)
        notifier = mock.Mock()
        with (
            mock.patch.object(
                discover, "refresh_source_metadata", side_effect=lambda event, *_: (event, "open")
            ) as refresh,
            mock.patch.object(rounds, "observe_exit", wraps=rounds.observe_exit) as observe_exit,
            self.assertRaisesRegex(work_items.WorkItemNotFoundError, "^task not found$"),
        ):
            self._retrigger(task_id, "new-retrigger", notifier)
        refresh.assert_not_called()
        notifier.assert_not_called()
        observe_exit.assert_not_called()
        self.assertEqual(self._retry_state(task_id), before)

    def test_adopted_pr_withholds_legacy_wakeup_replay(self):
        task_id = self._failed_github_task()
        notifier = mock.Mock()
        first = self._retrigger(task_id, "previous-retrigger", notifier)
        self.assertEqual(self._retrigger(task_id, "previous-retrigger", notifier), first)
        self._adopt_pr(task_id)
        before = self._retry_state(task_id)
        with (
            mock.patch.object(rounds, "observe_exit", wraps=rounds.observe_exit) as observe_exit,
            self.assertRaisesRegex(work_items.WorkItemNotFoundError, "^task not found$"),
        ):
            self._retrigger(task_id, "previous-retrigger", notifier)
        observe_exit.assert_not_called()
        notifier.assert_called_once()
        self.assertEqual(self._retry_state(task_id), before)

    def test_retrigger_writer_rechecks_pr_adoption_after_initial_read(self):
        task_id = self._failed_github_task()
        before = self._retry_state(task_id)
        original_observe = rounds.observe_exit

        def observe_and_adopt(conn, observed_id):
            evidence = original_observe(conn, observed_id)
            self._adopt_pr(observed_id)
            return evidence

        notifier = mock.Mock()
        with (
            mock.patch.object(rounds, "observe_exit", side_effect=observe_and_adopt) as observe_exit,
            self.assertRaisesRegex(work_items.WorkItemNotFoundError, "^task not found$"),
        ):
            self._retrigger(task_id, "raced-retrigger", notifier)
        observe_exit.assert_called_once()
        notifier.assert_not_called()
        self.assertEqual(self._retry_state(task_id), before)
