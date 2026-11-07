"""Execution recovery bounds both lineage depth and total recursive rows."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from robert_agent.persistence.queries import retries
from robert_agent.persistence.schema.database import init_database


class ExecutionRecoveryQueryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "state.sqlite3"
        init_database(self.db)
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute(
            """INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root)
            VALUES ('repo','example/repo','bot','main','/repo','/trees')"""
        )
        self.conn.execute(
            """INSERT INTO workstreams(workstream_id,repo_id,created_at,updated_at)
            VALUES ('stream','repo','2026-11-07','2026-11-07')"""
        )
        self.task("root", None, status="failed")

    def task(self, name, parent, *, status="completed", lineage="request"):
        self.conn.execute(
            """INSERT INTO tasks(task_id,workstream_id,parent_task_id,lifecycle,expected_output,
            created_at,updated_at,metadata_json) VALUES (?,'stream',?,?,'local_result','2026-11-07','2026-11-07',?)""",
            (name, parent, status, json.dumps({"supervision_lineage_key": lineage})),
        )

    def candidates(self):
        self.conn.commit()
        # Exercise the same connection adapter as production, not only sqlite3 fixtures.
        from robert_agent.persistence import database

        with closing(database.connect(self.db)) as conn:
            return [row[0] for row in retries.find_for_execution_recovery(conn, "root", "repo", "request")]

    def test_wide_lineage_stops_after_64_recursive_rows_including_root(self):
        for index in range(64):
            self.task(f"child-{index:03}", "root")
        for index in range(8):
            self.task(f"deep-{index:03}", "child-000")
        self.assertEqual(self.candidates(), [f"child-{index:03}" for index in range(8)])

    def test_linear_retries_keep_depth_limit_and_exclude_other_requests(self):
        parent = "root"
        for depth in range(1, 20):
            name = f"retry-{depth:02}"
            self.task(name, parent)
            parent = name
        self.task("different-request", "retry-15", lineage="other")
        self.assertEqual(self.candidates(), [f"retry-{depth:02}" for depth in range(16, 8, -1)])
