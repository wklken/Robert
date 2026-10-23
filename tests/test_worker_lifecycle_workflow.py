from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


class WorkerLifecycleWorkflowTests(unittest.TestCase):
    def _database(self):
        from robert_agent import storage, workflow_store

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "robert.sqlite3"
        storage.init_database(db_path)
        now = "2026-08-02T00:00:00+00:00"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES ('repo:test', 'example/test', 'bot', 'main', ?, ?)",
                (tmp.name, tmp.name),
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id, repo_id, lifecycle, created_at, updated_at) VALUES ('ws:test', 'repo:test', 'active', ?, ?)",
                (now, now),
            )
            conn.execute(
                "INSERT INTO tasks(task_id, workstream_id, task_kind, lifecycle, created_at, updated_at, metadata_json) VALUES ('task:test', 'ws:test', 'human_request', 'running', ?, ?, '{}')",
                (now, now),
            )
            conn.execute(
                "INSERT INTO attempts(attempt_id, task_id, attempt_no, status, metadata_json) VALUES ('attempt:test', 'task:test', 1, 'failed', ?)",
                (json.dumps({"dispatch": {"pid": 7}}),),
            )
            workflow_store.bind_subject(
                conn, "task", "task:test", "human_request", "legacy", now
            )
        return db_path

    def test_dead_recoverable_progress_consumes_quota_once_then_attention(self):
        from robert_agent import workflow_operations, workflow_store

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
                workflow_operations.classify_recovery(
                    conn, "task", "task:test", "attempt:test", evidence, now=now
                ),
                "recover_once",
            )
            self.assertEqual(
                workflow_store.get_binding(conn, "task", "task:test")[
                    "recovery_attempts"
                ],
                1,
            )
            self.assertEqual(
                workflow_operations.classify_recovery(
                    conn, "task", "task:test", "attempt:test", evidence, now=now
                ),
                "attention",
            )
            self.assertEqual(
                workflow_store.get_binding(conn, "task", "task:test")[
                    "binding_status"
                ],
                "attention",
            )

    def test_alive_same_never_consumes_recovery_quota(self):
        from robert_agent import workflow_operations, workflow_store

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
                workflow_operations.classify_recovery(
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
                workflow_store.get_binding(conn, "task", "task:test")[
                    "recovery_attempts"
                ],
                0,
            )


if __name__ == "__main__":
    unittest.main()
