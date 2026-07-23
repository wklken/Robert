from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import yaml

from robert_agent.init_config import init_config
from robert_agent.resource_files import resource
from robert_agent.route_config import resolve_route_config
from robert_agent.run_once import run_once


class RouteConfigTests(unittest.TestCase):
    def test_repo_override_replaces_only_explicit_fields(self):
        config = {
            "workers": {"default": {}, "reviewer": {}},
            "routes": {
                "new-pr": {
                    "worker": "default",
                    "required_skills": ["required-global"],
                    "recommended_skills": ["recommended-global"],
                }
            },
        }
        repo = {
            "routes": {
                "new-pr": {
                    "worker": "reviewer",
                    "recommended_skills": ["repo-skill"],
                }
            }
        }
        policy = {
            "id": "new-pr",
            "allowed_github_actions": ["push_existing_pr", "open_pr"],
            "workspace_mode": "new_branch",
        }

        result = resolve_route_config(config, repo, policy)

        self.assertEqual(result["worker"], "reviewer")
        self.assertEqual(result["required_skills"], ["required-global"])
        self.assertEqual(result["recommended_skills"], ["repo-skill"])
        self.assertEqual(
            result["allowed_github_actions"],
            ["push_existing_pr", "open_pr"],
        )

    def test_missing_required_skill_persists_blocked_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            config_path = root / "config.yml"
            init_config(
                config_path,
                {
                    "repo": "example/backend",
                    "repo_path": str(repo),
                    "worker": "codex",
                    "github_account": "robert-bot",
                    "trusted_actor": "maintainer",
                },
                non_interactive=True,
            )
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["data_dir"] = str(root / "data")
            config["routes"] = {
                "new-pr": {
                    "required_skills": ["missing-required"],
                }
            }
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
                                "body": "@robert-bot fix the timeout",
                                "intent": "bug_fix",
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
            self.assertEqual(result["dispatch_count"], 0)
            db_path = Path(config["data_dir"]).expanduser() / "robert.sqlite3"
            with closing(sqlite3.connect(db_path)) as conn:
                task_status = conn.execute(
                    "SELECT lifecycle FROM tasks"
                ).fetchone()[0]
                attempt_status, worktree_path = conn.execute(
                    "SELECT status, worktree_path FROM attempts"
                ).fetchone()
                notification = conn.execute(
                    "SELECT metadata_json FROM notifications "
                    "WHERE notification_type = 'required_route_skills_missing'"
                ).fetchone()[0]
            self.assertEqual(task_status, "failed")
            self.assertEqual(attempt_status, "failed")
            self.assertIsNone(worktree_path)
            self.assertIn("missing-required", notification)
