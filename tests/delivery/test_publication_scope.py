from contextlib import closing
import json
import sqlite3
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
)


class PublicationScopeTests(RepositoryCycleFixture):
    def test_publish_ready_actions_filters_by_repo_id(self):
        from robert_agent.delivery.application import publish
        from robert_agent.persistence.schema import database as storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        now = "2026-07-04T00:00:00+00:00"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            for repo_id, full_name in [("repo:a", "Org/a"), ("repo:b", "Org/b")]:
                conn.execute(
                    "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES (?, ?, 'robot', 'main', ?, ?)",
                    (repo_id, full_name, str(self.repo_root), str(self.worktree_root)),
                )
                source_id = f"source:{repo_id}"
                workstream_id = f"github:{full_name}#1"
                task_id = f"task:{repo_id}"
                result_id = f"result:{repo_id}"
                conn.execute(
                    "INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number) VALUES (?, ?, ?, 'issue', 1)",
                    (source_id, repo_id, workstream_id),
                )
                conn.execute(
                    "INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, lifecycle, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
                    (workstream_id, repo_id, source_id, now, now),
                )
                conn.execute(
                    "INSERT INTO tasks(task_id, workstream_id, lifecycle, created_at, updated_at) VALUES (?, ?, 'completed', ?, ?)",
                    (task_id, workstream_id, now, now),
                )
                conn.execute(
                    "INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, finished_at, metadata_json) VALUES (?, ?, 1, 'completed', ?, ?, '{\"result_contract\":\"compact-v1\"}')",
                    (f"attempt:{repo_id}", task_id, now, now),
                )
                conn.execute(
                    "INSERT INTO worker_results(result_id, task_id, attempt_id, output_type, created_at, metadata_json) VALUES (?, ?, ?, 'comment_analysis', ?, json_object('result_contract','compact-v1'))",
                    (result_id, task_id, f"attempt:{repo_id}", now),
                )
                conn.execute(
                    "INSERT INTO github_actions(action_id, result_id, task_id, action_type, target_url, audit_status, publish_status, created_at, metadata_json) VALUES (?, ?, ?, 'comment', ?, 'accepted', 'not_published', ?, ?)",
                    (
                        f"action:{repo_id}",
                        result_id,
                        task_id,
                        f"https://github.com/{full_name}/issues/1",
                        now,
                        json.dumps({"body": "[from-codex local repo] hi"}, sort_keys=True),
                    ),
                )

        result = publish.publish_ready_actions(db_path, dry_run=True, repo_id="repo:a")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["pending_count"], 1)
