from pathlib import Path
import sqlite3
import tempfile
import unittest

import yaml

from robert_agent import audit_result, publish, storage
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

    def test_legacy_markers_are_not_accepted_by_new_protocol(self):
        legacy_comment = "<!-- dd-comment task_id=1 -->"
        legacy_workstream = "<!-- dd-workstream task_id=1 -->"

        self.assertIsNone(
            audit_result.COMMENT_MARKER_RE.search(legacy_comment)
        )
        self.assertIsNone(
            audit_result.WORKSTREAM_MARKER_RE.search(legacy_workstream)
        )
        self.assertIsNone(
            publish.COMMENT_MARKER_RE.search(legacy_comment)
        )
        self.assertIsNone(
            publish.WORKSTREAM_MARKER_RE.search(legacy_workstream)
        )

    def test_database_init_adds_remediation_columns_to_legacy_schema(self):
        schema_path = self.root / "legacy-schema.sql"
        legacy_schema = storage.schema_sql().replace(
            "  actor_kind TEXT NOT NULL DEFAULT 'github_user',\n",
            "",
        ).replace(
            "  task_kind TEXT NOT NULL DEFAULT 'human_request' "
            "CHECK (task_kind IN ('human_request', 'ci_remediation', "
            "'merge_conflict_remediation')),\n",
            "",
        )
        schema_path.write_text(legacy_schema, encoding="utf-8")
        db_path = self.root / "legacy.sqlite3"

        storage.init_database(db_path, schema_path=schema_path)

        with sqlite3.connect(db_path) as conn:
            event_columns = {
                row[1]: row[4]
                for row in conn.execute("PRAGMA table_info(github_events)")
            }
            task_columns = {
                row[1]: row[4]
                for row in conn.execute("PRAGMA table_info(tasks)")
            }
        self.assertEqual(event_columns["actor_kind"], "'github_user'")
        self.assertEqual(task_columns["task_kind"], "'human_request'")

    def test_database_init_marks_dbos_workflow_migration_and_detects_checksum_drift(self):
        db_path = self.root / "workflow.sqlite3"
        storage.init_database(db_path)

        with sqlite3.connect(db_path) as conn:
            marker = conn.execute(
                "SELECT version, checksum FROM schema_migrations WHERE name = ?",
                (storage.WORKFLOW_MIGRATION_NAME,),
            ).fetchone()
            indexes = {
                row[1]
                for row in conn.execute("PRAGMA index_list(workflow_bindings)")
            }
        self.assertEqual(
            (storage.WORKFLOW_MIGRATION_VERSION, storage.WORKFLOW_MIGRATION_CHECKSUM),
            marker,
        )
        self.assertIn("idx_workflow_bindings_active", indexes)

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE schema_migrations SET checksum = 'wrong' WHERE name = ?",
                (storage.WORKFLOW_MIGRATION_NAME,),
            )
            conn.commit()
        with self.assertRaisesRegex(RuntimeError, "workflow migration checksum mismatch"):
            storage.init_database(db_path)

    def test_database_init_preserves_legacy_migration_versions(self):
        db_path = self.root / "legacy-markers.sqlite3"
        storage.init_database(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM schema_migrations WHERE version > 1")
            conn.execute(
                "INSERT INTO schema_migrations VALUES (2, ?, 'legacy-checksum', '2026-07-19')",
                ("dd-github-agent-work-item-control-v1",),
            )
            conn.execute(
                "INSERT INTO schema_migrations VALUES (3, ?, ?, '2026-07-23')",
                (storage.WORK_ITEM_MIGRATION_NAME, storage.WORK_ITEM_MIGRATION_CHECKSUM),
            )

        storage.init_database(db_path)
        storage.init_database(db_path)

        with sqlite3.connect(db_path) as conn:
            markers = conn.execute(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        self.assertEqual(markers[1], (2, "dd-github-agent-work-item-control-v1", "legacy-checksum"))
        self.assertEqual(markers[2][0], 3)
        self.assertEqual(markers[3], (4, storage.WORKFLOW_MIGRATION_NAME, storage.WORKFLOW_MIGRATION_CHECKSUM))
        self.assertEqual(len(markers), 6)
        self.assertEqual({row[1] for row in markers[4:]}, {
            storage.PR_MAINLINE_MIGRATION_NAME, storage.RECOMMENDED_SKILLS_MIGRATION_NAME,
        })
