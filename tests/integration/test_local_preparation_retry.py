"""Local preparation retries preserve the admitted task and durable wakeup."""

from contextlib import closing
import json
import sqlite3
import subprocess
from unittest import mock

from tests.support.board_control_integration import BoardControlIntegrationFixture
from robert_agent.work.application import commands
from robert_agent.execution import workspace
from robert_agent.execution.application import readiness
from robert_agent.foundation import values


class LocalPreparationRetryTests(BoardControlIntegrationFixture):
    def setUp(self):
        super().setUp()
        self.task = commands.create_work_item(
            self.db_path,
            context=self.context,
            repo_id=self.repo_id,
            title="Fix settings",
            description="Fix settings bug",
            priority="P1",
            routing_mode="auto",
            requested_worker=None,
            start=True,
            idempotency_key="local-retry",
        )["task_id"]

    def state(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute("SELECT lifecycle,metadata_json FROM tasks WHERE task_id=?", (self.task,)).fetchone()
            wakeup = conn.execute("SELECT status,not_before_at FROM wakeups WHERE task_id=?", (self.task,)).fetchone()
        return row[0], json.loads(row[1]).get("preparation_retry", {}), wakeup

    def test_transient_failure_backs_off_then_prepares_same_task(self):
        with mock.patch.object(
            workspace, "prepare_worktree", side_effect=subprocess.TimeoutExpired(["git", "fetch"], 1)
        ):
            self._run_local_cycle()
        state, retry, wakeup = self.state()
        self.assertEqual(state, "detected")
        self.assertEqual(retry["attempt_count"], 1)
        self.assertEqual(wakeup, ("pending", retry["next_retry_at"]))
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertFalse(readiness.has_local_work(conn, [self.repo_id], now=retry["failed_at"]))
        with (
            mock.patch.object(values, "utc_now", return_value=retry["failed_at"]),
            mock.patch.object(workspace, "prepare_worktree") as prepare,
        ):
            self._run_local_cycle()
            prepare.assert_not_called()
        with mock.patch.object(values, "utc_now", return_value=retry["next_retry_at"]):
            self._run_local_cycle()
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM attempts WHERE task_id=?", (self.task,)).fetchone()[0], 1
            )

    def test_retry_budget_stops_visibly(self):
        now = values.utc_now()
        for count in range(1, 4):
            with (
                mock.patch.object(values, "utc_now", return_value=now),
                mock.patch.object(
                    workspace, "prepare_worktree", side_effect=subprocess.TimeoutExpired(["git", "fetch"], 1)
                ),
            ):
                self._run_local_cycle()
            state, retry, wakeup = self.state()
            self.assertEqual(retry.get("attempt_count"), count)
            self.assertEqual(state, "detected" if count < 3 else "failed")
            now = retry["next_retry_at"]
        self.assertEqual(wakeup[0], "consumed")

    def test_slow_failure_backs_off_from_observation_time(self):
        started = "2026-11-08T00:00:00+00:00"
        failed = "2026-11-08T00:01:00+00:00"
        with mock.patch.object(values, "utc_now", return_value=started) as clock:

            def slow_fetch(*args, **kwargs):
                clock.return_value = failed
                raise subprocess.TimeoutExpired(["git", "fetch"], 60)

            with mock.patch.object(workspace, "prepare_worktree", side_effect=slow_fetch):
                self._run_local_cycle()
        _, retry, wakeup = self.state()
        self.assertEqual(retry["failed_at"], failed)
        self.assertGreater(values.parse_time(wakeup[1]), values.parse_time(failed))
