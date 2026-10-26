from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import storage, vote_store


DATA_MIGRATIONS = ('robert-pr-mainline-workstreams-v1', 'robert-route-recommended-skills-v1')


class StorageInitializationRemediationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Path(self.temporary.name) / 'legacy.sqlite3'
        with closing(sqlite3.connect(self.db)) as conn, conn:
            # A genuinely pre-initialization database, not new data inserted
            # after its migrations have already completed.
            conn.executescript(storage.schema_sql())
            conn.execute("INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('r','example/r','bot','main','/repo','/worktrees')")
            conn.execute("INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number,title,state,author_login) VALUES ('issue','r','github:example/r#1','issue',1,'Origin','open','maintainer'),('pr','r','github:example/r!2','pull_request',2,'Related','open','bot')")
            conn.execute("INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,lifecycle,active_task_id,created_at,updated_at) VALUES ('github:example/r#1','r','issue','active','t','2026-10-26','2026-10-26')")
            conn.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES ('t','github:example/r#1','queued','2026-10-26','2026-10-26')")
            payload = {'workstream_id': 'github:example/r!2', 'origin_workstream_id': 'github:example/r#1'}
            conn.execute("INSERT INTO github_events(event_id,repo_id,source_id,event_fingerprint,event_type,actor_login,authorization_status,event_at,payload_json) VALUES ('event','r','pr','comment:1','comment','maintainer','accepted_context','2026-10-26',?)", (json.dumps(payload),))
            conn.execute("INSERT INTO task_events(task_id,event_id,relationship,created_at) VALUES ('t','event','trigger','2026-10-26')")
            conn.execute("INSERT INTO route_decisions(route_decision_id,task_id,route_id,expected_output,required_skills_json,confidence,created_at) VALUES ('route','t','update-existing-pr','update_existing_pr','[\"legacy-skill\"]','high','2026-10-26')")

    def markers(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute('SELECT name FROM schema_migrations WHERE name IN (?,?) ORDER BY name', DATA_MIGRATIONS).fetchall()

    def test_legacy_data_is_converted_once_and_records_distinct_markers(self):
        storage.init_database(self.db)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT workstream_id FROM tasks WHERE task_id='t'").fetchone()[0], 'github:example/r!2')
            self.assertEqual(conn.execute("SELECT recommended_skills_json FROM route_decisions WHERE task_id='t'").fetchone()[0], '["legacy-skill"]')
            self.assertEqual(conn.execute('SELECT version FROM schema_migrations WHERE name=?', (storage.WORK_ITEM_MIGRATION_NAME,)).fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT version FROM schema_migrations WHERE name=?', (storage.WORKFLOW_MIGRATION_NAME,)).fetchone()[0], 3)
            self.assertIsNotNone(conn.execute("SELECT work_item_id FROM work_items WHERE workstream_id='github:example/r#1'").fetchone())
        self.assertEqual(self.markers(), [(name,) for name in DATA_MIGRATIONS])

    def test_reinitialization_does_not_rescan_or_rewrite_converted_history(self):
        storage.init_database(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE route_decisions SET recommended_skills_json='[]'")
            conn.execute("UPDATE workstreams SET lifecycle='canceled',active_task_id=NULL WHERE workstream_id='github:example/r!2'")
        statements = []
        original_connect = sqlite3.connect
        def traced(*args, **kwargs):
            conn = original_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn
        with patch.object(storage.sqlite3, 'connect', side_effect=traced):
            storage.init_database(self.db)
        normalized = [' '.join(sql.lower().split()) for sql in statements]
        self.assertFalse(any('select distinct t.task_id, t.lifecycle, gs.repo_id' in sql for sql in normalized))
        self.assertFalse(any(sql.startswith('update route_decisions set recommended_skills_json') for sql in normalized))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT recommended_skills_json FROM route_decisions').fetchone()[0], '[]')
            self.assertEqual(conn.execute("SELECT lifecycle FROM workstreams WHERE workstream_id='github:example/r!2'").fetchone()[0], 'canceled')

    def test_wrong_data_migration_checksum_is_rejected(self):
        storage.init_database(self.db)
        for name in DATA_MIGRATIONS:
            with self.subTest(migration=name), closing(sqlite3.connect(self.db)) as conn, conn:
                previous = conn.execute('SELECT checksum FROM schema_migrations WHERE name=?', (name,)).fetchone()
                if previous:
                    conn.execute("UPDATE schema_migrations SET checksum='wrong' WHERE name=?", (name,))
                else:
                    version = conn.execute('SELECT max(version)+1 FROM schema_migrations').fetchone()[0]
                    conn.execute("INSERT INTO schema_migrations VALUES (?,?,'wrong','2026-10-26')", (version, name))
            with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                storage.init_database(self.db)
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute('DELETE FROM schema_migrations WHERE name=?', (name,))
            storage.init_database(self.db)

    def test_interrupted_conversion_does_not_commit_marker_and_can_retry(self):
        migrate = storage._migrate_route_recommended_skills
        def fail_after_conversion(conn):
            migrate(conn)
            raise RuntimeError('interrupted conversion')
        with patch.object(storage, '_migrate_route_recommended_skills', side_effect=fail_after_conversion):
            with self.assertRaisesRegex(RuntimeError, 'interrupted conversion'):
                storage.init_database(self.db)
        self.assertEqual(self.markers(), [])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT workstream_id FROM tasks WHERE task_id='t'").fetchone()[0], 'github:example/r#1')
        storage.init_database(self.db)
        self.assertEqual(self.markers(), [(name,) for name in DATA_MIGRATIONS])

    def test_late_initialization_failure_does_not_record_completion_early(self):
        with patch.object(vote_store, 'initialize', side_effect=RuntimeError('late failure')):
            with self.assertRaisesRegex(RuntimeError, 'late failure'):
                storage.init_database(self.db)
        self.assertEqual(self.markers(), [])
        storage.init_database(self.db)
        self.assertEqual(self.markers(), [(name,) for name in DATA_MIGRATIONS])
