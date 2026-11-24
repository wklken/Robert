"""Reusable hot statements must bind each request and observe current rows."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from robert_agent.persistence import database
from robert_agent.persistence.queries import attempts, tasks
from robert_agent.persistence.schema import database as schema


class HotQueryTests(unittest.TestCase):
    def test_repeated_queries_keep_parameters_and_database_state_current(self):
        for connect in (sqlite3.connect, database.connect):
            with self.subTest(connection=connect.__module__), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "state.db"
                schema.init_database(path)
                with closing(sqlite3.connect(path)) as seed, seed:
                    for name in ("one", "two"):
                        seed.execute(
                            "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch,"
                            "repo_root, worktree_root) VALUES (?, ?, 'bot', 'main', '/repo', '/trees')",
                            (name, f"org/{name}"),
                        )
                        seed.execute(
                            "INSERT INTO workstreams(workstream_id, repo_id, created_at, updated_at)"
                            " VALUES (?, ?, 'now', 'now')",
                            (name, name),
                        )
                        seed.execute(
                            "INSERT INTO tasks(task_id, workstream_id, lifecycle, created_at, updated_at)"
                            " VALUES (?, ?, 'running', 'now', 'now')",
                            (name, name),
                        )
                        seed.execute(
                            "INSERT INTO attempts(attempt_id, task_id, attempt_no, status) VALUES (?, ?, 1, 'running')",
                            (name, name),
                        )
                with closing(connect(path)) as conn:
                    for selected in (["one"], ["two", "one"], [], ["two"]):
                        self.assertEqual(
                            attempts.running_attempt_count_for_repos_filtered(conn, selected)[0][0], len(selected)
                        )
                    for task, attempt, expected in (
                        ("one", "one", [("running", "running")]),
                        ("two", "one", []),
                        ("two", "two", [("running", "running")]),
                    ):
                        self.assertEqual(tasks.find_for_attempt_is_active(conn, task, attempt), expected)
                    for repo in ("one", "two", "missing", "one"):
                        self.assertEqual(
                            attempts.find_for_running_attempt_count(conn, repo)[0][0], int(repo != "missing")
                        )
                with closing(sqlite3.connect(path)) as writer, writer:
                    writer.execute("UPDATE tasks SET lifecycle='completed' WHERE task_id='two'")
                    writer.execute(
                        "INSERT INTO attempts(attempt_id, task_id, attempt_no, status) VALUES ('new', 'one', 2, 'prepared')"
                    )
                with closing(connect(path)) as conn:
                    self.assertEqual(attempts.find_for_running_attempt_count(conn, "two")[0][0], 0)
                    self.assertEqual(attempts.running_attempt_count_for_repos_filtered(conn, ["one", "two"])[0][0], 1)
                    self.assertEqual(tasks.find_for_attempt_is_active(conn, "one", "one"), [])
                    self.assertEqual(tasks.find_for_attempt_is_active(conn, "one", "new"), [("running", "prepared")])
