"""Shared fixtures for board control integration tests."""

from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT


class BoardControlIntegrationFixture(unittest.TestCase):
    def setUp(self):
        from robert_agent.persistence.schema import database as storage
        from robert_agent.work.application import commands as work_items

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo_root = self.root / "repo"
        self.repo_root.mkdir()
        (self.repo_root / ".git").mkdir()
        self.worktree_root = self.repo_root / ".worktrees"
        self.data_dir = self.root / "data"
        self.db_path = self.data_dir / "dd.sqlite3"
        self.repo_id = "repo:example/repo"
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "data_dir": str(self.data_dir),
                    "database": "dd.sqlite3",
                    "github": {"account": "robot"},
                    "default_worker": "default",
                    "workers": {
                        "default": {"adapter": "cbc", "command": "cbc", "model": "default-model", "effort": "medium"},
                        "reviewer": {"adapter": "codex", "command": "codex", "model": "review-model", "effort": "high"},
                    },
                    "scheduler": {"max_concurrency": 2},
                    "repos": [
                        {
                            "full_name": "example/repo",
                            "github_account": "robot",
                            "trusted_actors": ["owner"],
                            "max_concurrency": 2,
                            "checkout": str(self.repo_root),
                            "worktrees": str(self.worktree_root),
                            "default_branch": "main",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.empty_fixture = self.root / "empty.json"
        self.empty_fixture.write_text('{"events": []}', encoding="utf-8")
        storage.init_database(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES (?, 'example/repo', 'robot', 'main', ?, ?)",
                (self.repo_id, str(self.repo_root), str(self.worktree_root)),
            )
        self.context = work_items.CommandContext(
            actor_kind="operator",
            actor_identity="owner",
            allowed_repo_ids=frozenset({self.repo_id}),
            allowed_workers=frozenset({"default", "reviewer"}),
        )

    def _run_local_cycle(self):
        from robert_agent.runtime import cycle as run_once

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.empty_fixture,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(result["ok"], result)
        return result

    def _finish_task(self, task_id, now):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET status = 'completed', finished_at = ? WHERE task_id = ?",
                (now, task_id),
            )
            conn.execute(
                "UPDATE tasks SET lifecycle = 'completed', updated_at = ? WHERE task_id = ?",
                (now, task_id),
            )
