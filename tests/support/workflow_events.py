"""Shared fixtures for workflow events tests."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from robert_agent.adapters.sqlite import database as storage
from robert_agent.adapters.sqlite import execution_control as execution_store


NOW = "2026-08-02T00:00:00+00:00"


def _fact(**overrides):
    fact = {
        "fact_id": "event:comment-1",
        "subject_type": "task",
        "subject_id": "task-1",
        "repo": "example/repo",
        "source_type": "pull_request",
        "source_key": "github:example/repo!7",
        "number": 7,
        "event_type": "comment",
        "authorization_status": "accepted_context",
        "actor_login": "reviewer",
        "body": "please address this",
    }
    fact.update(overrides)
    return fact


class WorkflowEventApplicationFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "robert.sqlite3"
        storage.init_database(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES ('repo-1', 'example/repo', 'bot', 'main', '/repo', '/worktrees')"
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id, repo_id, lifecycle, active_task_id, created_at, updated_at) VALUES ('ws-1', 'repo-1', 'active', 'task-1', ?, ?)",
                (NOW, NOW),
            )
            conn.execute(
                "INSERT INTO tasks(task_id, workstream_id, task_kind, lifecycle, created_at, updated_at) VALUES ('task-1', 'ws-1', 'human_request', 'running', ?, ?)",
                (NOW, NOW),
            )

    def _bind(self, owner):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            return execution_store.ensure_subject(
                conn, "task", "task-1", "human_request", NOW
            )
