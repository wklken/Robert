from contextlib import closing
import sqlite3
from robert_agent.execution import capacity
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
)


class CapacityTests(RepositoryCycleFixture):
    def test_running_attempt_count_for_repos_counts_all_configured_repos(self):
        from robert_agent.persistence.schema import database as storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        now = "2026-07-04T00:00:00+00:00"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            for repo_id, full_name in [("repo:a", "Org/a"), ("repo:b", "Org/b"), ("repo:c", "Org/c")]:
                conn.execute(
                    "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES (?, ?, 'robot', 'main', ?, ?)",
                    (repo_id, full_name, str(self.repo_root), str(self.worktree_root)),
                )
                source_id = f"source:{repo_id}"
                workstream_id = f"github:{full_name}#1"
                task_id = f"task:{repo_id}"
                attempt_id = f"attempt:{repo_id}"
                conn.execute(
                    "INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number) VALUES (?, ?, ?, 'issue', 1)",
                    (source_id, repo_id, workstream_id),
                )
                conn.execute(
                    "INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, lifecycle, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
                    (workstream_id, repo_id, source_id, now, now),
                )
                lifecycle = "running" if repo_id != "repo:c" else "completed"
                conn.execute(
                    "INSERT INTO tasks(task_id, workstream_id, lifecycle, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (task_id, workstream_id, lifecycle, now, now),
                )
                conn.execute(
                    "INSERT INTO attempts(attempt_id, task_id, attempt_no, status, metadata_json) VALUES (?, ?, 1, 'running', '{\"result_contract\":\"compact-v1\"}')",
                    (attempt_id, task_id),
                )
            count = capacity.running_attempt_count_for_repos(conn, ["repo:a", "repo:b"])

        self.assertEqual(count, 2)
