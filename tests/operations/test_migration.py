from pathlib import Path
import sqlite3
import tempfile
import unittest


from robert_agent.delivery.application import audit as audit_result
from robert_agent.delivery.application import effects as publication_effects
from robert_agent.persistence.schema import database as storage


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_legacy_markers_are_not_accepted_by_new_protocol(self):
        legacy_comment = "<!-- dd-comment task_id=1 -->"
        legacy_workstream = "<!-- dd-workstream task_id=1 -->"

        self.assertIsNone(audit_result.COMMENT_MARKER_RE.search(legacy_comment))
        self.assertIsNone(audit_result.WORKSTREAM_MARKER_RE.search(legacy_workstream))
        self.assertIsNone(publication_effects.COMMENT_MARKER_RE.search(legacy_comment))
        self.assertIsNone(publication_effects.WORKSTREAM_MARKER_RE.search(legacy_workstream))

    def test_database_init_adds_remediation_columns_to_legacy_schema(self):
        schema_path = self.root / "legacy-schema.sql"
        legacy_schema = (
            storage.schema_sql()
            .replace(
                "  actor_kind TEXT NOT NULL DEFAULT 'github_user',\n",
                "",
            )
            .replace(
                "  task_kind TEXT NOT NULL DEFAULT 'human_request' "
                "CHECK (task_kind IN ('human_request', 'ci_remediation', "
                "'merge_conflict_remediation')),\n",
                "",
            )
        )
        schema_path.write_text(legacy_schema, encoding="utf-8")
        db_path = self.root / "legacy.sqlite3"

        storage.init_database(db_path, schema_path=schema_path)

        with sqlite3.connect(db_path) as conn:
            event_columns = {row[1]: row[4] for row in conn.execute("PRAGMA table_info(github_events)")}
            task_columns = {row[1]: row[4] for row in conn.execute("PRAGMA table_info(tasks)")}
        self.assertEqual(event_columns["actor_kind"], "'github_user'")
        self.assertEqual(task_columns["task_kind"], "'human_request'")

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
            markers = conn.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version").fetchall()
        self.assertEqual(markers[1], (2, "dd-github-agent-work-item-control-v1", "legacy-checksum"))
        self.assertEqual(markers[2][0], 3)
        self.assertEqual(len(markers), 6)
        self.assertEqual(
            {row[1] for row in markers[3:]},
            {
                storage.PR_MAINLINE_MIGRATION_NAME,
                storage.RECOMMENDED_SKILLS_MIGRATION_NAME,
                storage.SUPERVISION_MIGRATION_NAME,
            },
        )
        self.assertEqual(markers[-1][1:], (storage.SUPERVISION_MIGRATION_NAME, storage.SUPERVISION_MIGRATION_CHECKSUM))
