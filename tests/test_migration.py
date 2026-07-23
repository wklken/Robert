from pathlib import Path
import sqlite3
import tempfile
import unittest

import yaml

from robert_agent import audit_result, publish
from robert_agent.migrate import migrate_legacy


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "dd-github-agent"
        self.target = self.root / "robert"
        self.source.mkdir()
        (self.source / "config.yml").write_text(
            yaml.safe_dump(
                {
                    "data_dir": str(self.source),
                    "database": "dd.sqlite3",
                    "dd_account": "legacy-bot",
                    "workers": [
                        {
                            "name": "default",
                            "agent": "codex",
                            "command": "codex",
                            "default_model": "configured-model",
                            "default_effort": "high",
                        }
                    ],
                    "route_worker_models": {
                        "review-pr": {
                            "worker": "default",
                            "model": "configured-model",
                            "effort": "high",
                        }
                    },
                    "repos": [],
                }
            ),
            encoding="utf-8",
        )
        with sqlite3.connect(self.source / "dd.sqlite3") as conn:
            conn.execute(
                "CREATE TABLE repos("
                "repo_id TEXT PRIMARY KEY, "
                "full_name TEXT, "
                "dd_account TEXT, "
                "default_base_branch TEXT, "
                "repo_root TEXT, "
                "worktree_root TEXT, "
                "enabled INTEGER)"
            )

    def test_dry_run_does_not_create_target(self):
        result = migrate_legacy(self.source, self.target, dry_run=True)
        self.assertTrue(result["ok"])
        self.assertFalse(self.target.exists())

    def test_migration_renames_config_and_schema(self):
        result = migrate_legacy(self.source, self.target)
        self.assertTrue(result["ok"])
        config = yaml.safe_load(
            (self.target / "config.yml").read_text(encoding="utf-8")
        )
        self.assertEqual(config["github"]["account"], "legacy-bot")
        self.assertEqual(
            config["routes"]["review-pr"]["worker"],
            "default",
        )
        with sqlite3.connect(self.target / "robert.sqlite3") as conn:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(repos)")
            }
        self.assertIn("github_account", columns)
        self.assertNotIn("dd_account", columns)

    def test_legacy_markers_remain_dedupe_compatible(self):
        legacy_comment = "<!-- dd-comment task_id=1 -->"
        legacy_workstream = "<!-- dd-workstream task_id=1 -->"

        self.assertIsNotNone(
            audit_result.COMMENT_MARKER_RE.search(legacy_comment)
        )
        self.assertIsNotNone(
            audit_result.WORKSTREAM_MARKER_RE.search(legacy_workstream)
        )
        self.assertIsNotNone(
            publish.COMMENT_MARKER_RE.search(legacy_comment)
        )
        self.assertIsNotNone(
            publish.WORKSTREAM_MARKER_RE.search(legacy_workstream)
        )
