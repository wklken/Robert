from contextlib import closing
import sqlite3
from tests.support.temp_workspace import TemporaryWorkspace

from robert_agent.persistence.schema import database as storage
from robert_agent.persistence.stores import wakeups as wakeup_store
from robert_agent.work import wakeup


class CanceledTaskWakeupTests(TemporaryWorkspace):
    def test_request_retirement_is_scoped_and_uses_the_callers_transaction(self):
        db = self.root / "wakeup.sqlite3"
        storage.init_database(db)
        now = "2026-11-21T00:00:00+00:00"
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) "
                "VALUES ('repo','example/repo','bot','main','/repo','/worktrees')"
            )
            for task_id in ("task", "other"):
                conn.execute(
                    "INSERT INTO workstreams(workstream_id,repo_id,lifecycle,created_at,updated_at) "
                    "VALUES (?,'repo','active',?,?)",
                    (task_id, now, now),
                )
                conn.execute(
                    "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) "
                    "VALUES (?,?,'detected',?,?)",
                    (task_id, task_id, now, now),
                )
            requests = {}
            for name, task_id, reason, kind in (
                ("ordinary", "task", "manual_operator_request", ""),
                ("retrigger", "task", "manual_operator_request", "github_retrigger"),
                ("result", "task", "worker_result_ready", ""),
                ("other", "other", "manual_operator_request", ""),
            ):
                requests[name] = wakeup.request_wakeup(
                    conn,
                    repo_id="repo",
                    task_id=task_id,
                    reason=reason,
                    dedupe_key=name,
                    metadata={"kind": kind},
                    now=now,
                )["wakeup_id"]
            conn.commit()
            self.assertEqual(wakeup_store.cancel_terminal_task_requests(conn, "task", now), 0)
            conn.execute("UPDATE tasks SET lifecycle='canceled' WHERE task_id='task'")
            self.assertEqual(wakeup_store.cancel_terminal_task_requests(conn, "task", now), 1)
            statuses = dict(conn.execute("SELECT wakeup_id,status FROM wakeups"))
            self.assertEqual(statuses[requests["ordinary"]], "canceled")
            for name in ("retrigger", "result", "other"):
                self.assertEqual(statuses[requests[name]], "pending")
            self.assertTrue(conn.in_transaction)
            conn.rollback()
            self.assertEqual(conn.execute("SELECT lifecycle FROM tasks WHERE task_id='task'").fetchone()[0], "detected")
            self.assertEqual(
                conn.execute("SELECT status FROM wakeups WHERE wakeup_id=?", (requests["ordinary"],)).fetchone()[0],
                "pending",
            )
