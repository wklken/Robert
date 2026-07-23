from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import yaml

from robert_agent.init_config import init_config
from robert_agent.resource_files import resource
from robert_agent.run_once import run_once
from robert_agent.worktree import resolve_task_workspace


class WorkspaceResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo_root = Path(self.tmp.name) / "backend"
        self.repo_root.mkdir()
        (self.repo_root / ".git").mkdir()
        self.repo = {
            "full_name": "example/backend",
            "repo_root": str(self.repo_root),
            "worktree_root": str(self.repo_root / ".worktrees"),
            "default_base_branch": "main",
        }

    def test_new_pr_uses_selected_repo_worktree_root(self):
        result = resolve_task_workspace(
            self.repo,
            {"id": "new-pr", "workspace_mode": "new_branch"},
            {"number": 42, "branch_slug": "fix-timeout"},
            dry_run=True,
        )
        self.assertEqual(result["mode"], "new_branch")
        self.assertTrue(
            result["worktree_path"].startswith(
                str(self.repo_root / ".worktrees")
            )
        )

    def test_classification_uses_artifact_directory(self):
        result = resolve_task_workspace(
            self.repo,
            {"id": "classification-result", "workspace_mode": "none"},
            {
                "number": 42,
                "artifact_dir": str(Path(self.tmp.name) / "task"),
            },
            dry_run=True,
        )
        self.assertEqual(result["mode"], "none")
        self.assertEqual(
            result["worktree_path"],
            str(Path(self.tmp.name) / "task"),
        )

    def test_repositories_resolve_independent_worktree_roots(self):
        frontend_root = Path(self.tmp.name) / "frontend"
        frontend_root.mkdir()
        (frontend_root / ".git").mkdir()
        frontend = {
            **self.repo,
            "full_name": "example/frontend",
            "repo_root": str(frontend_root),
            "worktree_root": str(frontend_root / ".worktrees"),
        }
        route = {"id": "new-pr", "workspace_mode": "new_branch"}
        source = {"number": 42, "branch_slug": "fix-timeout"}

        backend_result = resolve_task_workspace(
            self.repo,
            route,
            source,
            dry_run=True,
        )
        frontend_result = resolve_task_workspace(
            frontend,
            route,
            source,
            dry_run=True,
        )

        self.assertTrue(
            backend_result["worktree_path"].startswith(
                self.repo["worktree_root"]
            )
        )
        self.assertTrue(
            frontend_result["worktree_path"].startswith(
                frontend["worktree_root"]
            )
        )

    def test_public_analysis_route_records_isolated_workspace(self):
        root = Path(self.tmp.name)
        config_path = root / "config.yml"
        init_config(
            config_path,
            {
                "repo": "example/backend",
                "repo_path": str(self.repo_root),
                "worker": "codex",
                "github_account": "robert-bot",
                "trusted_actor": "maintainer",
            },
            non_interactive=True,
        )
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["data_dir"] = str(root / "data")
        config_path.write_text(
            yaml.safe_dump(config, sort_keys=False),
            encoding="utf-8",
        )
        fixture = root / "fixture.json"
        fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "issue-42",
                            "number": 42,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "maintainer",
                            "body": "@robert-bot analyze the timeout",
                            "intent": "analysis",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = run_once(
            config_path,
            workflow_path=resource("workflow.yml"),
            fixture_path=fixture,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        with closing(
            sqlite3.connect(root / "data" / "robert.sqlite3")
        ) as conn:
            worktree_path = conn.execute(
                "SELECT worktree_path FROM attempts"
            ).fetchone()[0]
        self.assertEqual(
            worktree_path,
            str(self.repo_root / ".worktrees" / "analysis-42"),
        )
