from contextlib import closing
import sqlite3
import unittest

from robert_agent.persistence.schema.database import schema_sql
from robert_agent.work.queries import sources_for_workstream


class WorkstreamSourceQueryTests(unittest.TestCase):
    def test_scope_includes_primary_and_linked_sources_without_unrelated_sources(self):
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.executescript(schema_sql())
            conn.execute(
                """INSERT INTO repos
                (repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root)
                VALUES ('repo-1', 'owner/repo', 'robot', 'main', '/repo', '/repo/.worktrees')"""
            )
            conn.executemany(
                """INSERT INTO github_sources (source_id, repo_id, source_key, source_type, number)
                VALUES (?, 'repo-1', ?, ?, ?)""",
                [
                    ("issue", "owner/repo#4", "issue", 4),
                    ("pr", "owner/repo!4", "pull_request", 4),
                    ("other", "owner/repo#8", "issue", 8),
                ],
            )
            conn.executemany(
                """INSERT INTO workstreams
                (workstream_id, repo_id, primary_source_id, created_at, updated_at)
                VALUES (?, 'repo-1', ?, 'now', 'now')""",
                [("work-1", "issue"), ("work-2", "other")],
            )
            conn.executemany(
                """INSERT INTO workstream_sources (workstream_id, source_id, relationship, created_at)
                VALUES ('work-1', ?, ?, 'now')""",
                [("issue", "primary"), ("pr", "derived_pr")],
            )

            self.assertEqual(
                sources_for_workstream(conn, "work-1"),
                [{"source_type": "issue", "number": 4}, {"source_type": "pull_request", "number": 4}],
            )
            self.assertEqual(sources_for_workstream(conn, "missing"), [])
