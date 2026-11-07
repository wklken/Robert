"""Shared fixtures for publication reconcile tests."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from robert_agent.adapters.sqlite import database as storage


class Completed:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class PublicationReconcileFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "robert.sqlite3"
        storage.init_database(self.db_path)
        now = "2026-08-02T00:00:00+00:00"
        scope = {
            "repo": "x/y",
            "sources": [{"source_type": "issue", "number": 1}],
        }
        metadata = {"audit": {"status": "accepted", "action_scope": scope}}
        body = "<!-- robert-comment task_id=task-1 attempt_id=attempt-1 action_index=0 -->\nready"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES ('repo-1', 'x/y', 'bot', 'main', '/repo', '/worktrees')"
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id, repo_id, lifecycle, created_at, updated_at) VALUES ('ws-1', 'repo-1', 'active', ?, ?)",
                (now, now),
            )
            conn.execute(
                "INSERT INTO tasks(task_id, workstream_id, lifecycle, created_at, updated_at) VALUES ('task-1', 'ws-1', 'queued', ?, ?)",
                (now, now),
            )
            conn.execute(
                "INSERT INTO attempts(attempt_id, task_id, attempt_no, status) VALUES ('attempt-1', 'task-1', 1, 'completed')"
            )
            conn.execute(
                "INSERT INTO worker_results(result_id, task_id, attempt_id, output_type, created_at, metadata_json) VALUES ('result-1', 'task-1', 'attempt-1', 'comment_analysis', ?, json_set(?, '$.result_contract', 'compact-v1'))",
                (now, json.dumps(metadata, sort_keys=True)),
            )
            conn.execute(
                "INSERT INTO github_actions(action_id, result_id, task_id, action_type, target_url, audit_status, publish_status, created_at, metadata_json) VALUES ('action-1', 'result-1', 'task-1', 'comment', 'https://github.com/x/y/issues/1', 'accepted', 'not_published', ?, ?)",
                (now, json.dumps({"body": body}, sort_keys=True)),
            )
        self.body = body
