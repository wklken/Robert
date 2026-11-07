"""Shared fixtures for worker lifecycle workflow tests."""
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


class WorkerLifecycleWorkflowFixture(unittest.TestCase):
    def _database(self):
        from robert_agent.adapters.sqlite import database as storage
        from robert_agent.adapters.sqlite import execution_control as execution_store

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
            execution_store.ensure_subject(conn, 'task', 'task:test', 'human_request', now)
        return db_path
