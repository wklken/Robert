from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT

WORKER_MODULE = PACKAGE_ROOT / "worker"


class ConfigAndSchemaTests(unittest.TestCase):
    def test_route_completion_uses_covering_index_after_existing_database_upgrade(self):
        from robert_agent.persistence.schema import database as storage

        db_path = self.root / "upgrade.sqlite3"
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("DROP INDEX IF EXISTS idx_run_repo_steps_consumer_completed")
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn:
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT max(finished_at) FROM run_repo_steps "
                "WHERE repo_id=? AND step_key='route' AND status='succeeded'",
                ("repo",),
            ).fetchall()
        self.assertIn("COVERING INDEX idx_run_repo_steps_consumer_completed", str(plan))

    def test_delivery_recovery_upgrade_preserves_paused_owner_lifecycle(self):
        from robert_agent.persistence.schema import database as storage

        db_path = self.root / "recovery-upgrade.sqlite3"
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("DROP INDEX idx_source_delivery_recovery_active")
            conn.execute("ALTER TABLE source_delivery_recoveries DROP COLUMN suspended_at")
            conn.execute(
                "CREATE UNIQUE INDEX idx_source_delivery_recovery_active ON source_delivery_recoveries(source_id) WHERE completed_at IS NULL"
            )
        storage.init_database(db_path)
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(source_delivery_recoveries)")}
            self.assertIn("suspended_at", columns)
            [definition] = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='idx_source_delivery_recovery_active'"
            ).fetchone()
            self.assertIn("completed_at IS NULL AND suspended_at IS NULL", definition)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    expected_tables = {
        "schema_migrations",
        "repos",
        "actors",
        "actor_permissions",
        "sources",
        "source_events",
        "workstreams",
        "workstream_sources",
        "work_items",
        "work_item_events",
        "tasks",
        "task_events",
        "ingress_requests",
        "source_github_ingress",
        "source_github_observations",
        "request_events",
        "source_sessions",
        "source_rounds",
        "source_resume_transitions",
        "source_delivery_pauses",
        "source_delivery_recoveries",
        "source_pr_selections",
        "source_round_requests",
        "source_round_tasks",
        "source_model_runs",
        "route_decisions",
        "attempts",
        "worker_phases",
        "worker_results",
        "github_actions",
        "pr_remediation_episodes",
        "pr_ci_observations",
        "artifacts",
        "notifications",
        "wakeups",
        "agent_runs",
        "run_steps",
        "run_repo_steps",
        "leases",
        "audit_events",
        "project_memory_entries",
        "project_memory_revisions",
        "project_memory_terms",
        "knowledge_candidates",
        "runtime_knowledge",
        "execution_subjects",
        "execution_fact_receipts",
        "publication_intents",
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo_root = self.root / "blueking-apigateway"
        self.repo_root.mkdir()
        (self.repo_root / ".git").mkdir()
        self.worktree_root = self.repo_root / ".worktrees"
        self.worktree_root.mkdir()
        self.data_dir = self.root / "data"

    def test_fresh_database_init_creates_expected_tables_and_is_idempotent(self):
        from robert_agent.persistence.schema import database as storage

        db_path = self.data_dir / "dd.sqlite3"
        first = storage.init_database(db_path)
        second = storage.init_database(db_path)

        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertTrue(db_path.exists())
        self.assertFalse((self.data_dir / "dispatcher.sqlite3").exists())
        with closing(sqlite3.connect(db_path)) as conn, conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
            migrations = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
        self.assertTrue(self.expected_tables.issubset(tables))
        self.assertEqual(
            tables
            & {
                "workflow_bindings",
                "workflow_outbox",
                "workflow_signal_receipts",
                "workflow_shadow_observations",
                "execution_pending_facts",
                "execution_migrations",
            },
            set(),
        )
        self.assertNotIn("robert-dbos-workflow-v1", migrations)
        self.assertIn("idx_workstreams_one_active_task", indexes)
        self.assertIn("idx_leases_one_active_resource", indexes)
        self.assertIn("idx_wakeups_status_due", indexes)
        self.assertIn("idx_wakeups_result", indexes)

    def test_schema_includes_daemon_tables_and_indexes(self):
        from robert_agent.persistence.schema import database as storage

        db_path = self.root / "daemon-schema.sqlite3"
        storage.init_database(db_path)

        with closing(sqlite3.connect(db_path)) as conn, conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}

        self.assertIn("daemon_runs", tables)
        self.assertIn("daemon_events", tables)
        self.assertIn("idx_daemon_events_run_created", indexes)
        self.assertIn("idx_daemon_events_type_created", indexes)

    def test_schema_includes_origin_workstream_link(self):
        from robert_agent.persistence.schema import database as storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(workstreams)")}
        self.assertIn("origin_workstream_id", columns)

    def test_github_actions_track_publish_status_separately_from_audit(self):
        from robert_agent.persistence.schema import database as storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(github_actions)")}
        self.assertIn("publish_status", columns)

    def test_schema_includes_remediation_contracts_and_unique_identities(self):
        from robert_agent.persistence.schema import database as storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            event_columns = {row[1]: row[4] for row in conn.execute("PRAGMA table_info(source_events)")}
            task_columns = {row[1]: row[4] for row in conn.execute("PRAGMA table_info(tasks)")}
            conn.execute(
                """
                INSERT INTO repos(
                  repo_id, full_name, github_account, default_base_branch,
                  repo_root, worktree_root
                )
                VALUES ('repo-1', 'example/backend', 'robert-bot', 'main', '/repo', '/worktrees')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, lifecycle, created_at, updated_at
                )
                VALUES ('ws-1', 'repo-1', 'completed', '2026-07-26', '2026-07-26')
                """
            )
            episode = (
                "episode-1",
                "ws-1",
                "ci",
                "head:base",
                "head",
                "base",
                "open",
                "2026-07-26",
                "2026-07-26",
            )
            conn.execute(
                """
                INSERT INTO pr_remediation_episodes(
                  episode_id, workstream_id, episode_kind, subject_key,
                  observed_head_sha, observed_base_sha, status,
                  first_seen_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                episode,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO pr_remediation_episodes(
                      episode_id, workstream_id, episode_kind, subject_key,
                      observed_head_sha, observed_base_sha, status,
                      first_seen_at, updated_at
                    )
                    VALUES ('episode-2', 'ws-1', 'ci', 'head:base',
                            'head', 'base', 'open', '2026-07-26', '2026-07-26')
                    """
                )
            observation = (
                "observation-1",
                "episode-1",
                "workflow_run",
                "123",
                1,
                "unit",
                "completed",
            )
            conn.execute(
                """
                INSERT INTO pr_ci_observations(
                  observation_id, episode_id, source_kind, external_id,
                  attempt_no, check_name, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                observation,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO pr_ci_observations(
                      observation_id, episode_id, source_kind, external_id,
                      attempt_no, check_name, status
                    )
                    VALUES ('observation-2', 'episode-1', 'workflow_run',
                            '123', 1, 'unit', 'completed')
                    """
                )

        self.assertEqual(event_columns["actor_kind"], "'github_user'")
        self.assertEqual(task_columns["task_kind"], "'human_request'")

    def test_schema_includes_project_memory_tables_and_lookup_index(self):
        from robert_agent.persistence.schema import database as storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
            entry_columns = {row[1] for row in conn.execute("PRAGMA table_info(project_memory_entries)")}
        self.assertTrue(
            {
                "project_memory_entries",
                "project_memory_revisions",
                "project_memory_terms",
            }.issubset(tables)
        )
        self.assertIn("idx_project_memory_terms_lookup", indexes)
        self.assertIn("memory_thread_key", entry_columns)
        self.assertIn("short_summary", entry_columns)

    def test_schema_includes_runtime_knowledge_tables(self):
        from robert_agent.persistence.schema import database as storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
            candidate_columns = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_candidates)")}
            runtime_columns = {row[1] for row in conn.execute("PRAGMA table_info(runtime_knowledge)")}
        self.assertTrue({"knowledge_candidates", "runtime_knowledge"}.issubset(tables))
        self.assertIn("idx_knowledge_candidates_status", indexes)
        self.assertIn("idx_runtime_knowledge_lookup", indexes)
        self.assertIn("status", candidate_columns)
        self.assertIn("scope_type", runtime_columns)
        self.assertIn("retrieval_boost_json", runtime_columns)


class SourceResultOwnerMigrationTests(unittest.TestCase):
    def test_action_owner_cutover_preserves_receipts_and_rolls_back_on_unproved_effect(self):
        from robert_agent.persistence.schema.database import init_database

        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "action-owner.sqlite3"
            init_database(path)
            with closing(sqlite3.connect(path)) as conn, conn:
                current_action_sql = conn.execute(
                    "SELECT sql FROM sqlite_schema WHERE name='github_actions'"
                ).fetchone()[0]
                action_columns = tuple(
                    entry[1]
                    for entry in conn.execute("PRAGMA table_info(github_actions)")
                    if entry[1] != "source_result_id"
                )
                old_action_sql = "\n".join(
                    line.replace("task_id TEXT REFERENCES", "task_id TEXT NOT NULL REFERENCES")
                    for line in current_action_sql.splitlines()
                    if "source_result_id" not in line and "CHECK (task_id IS NOT NULL OR" not in line
                ).replace("DEFAULT '{}',\n)", "DEFAULT '{}'\n)")
                conn.execute("DROP TABLE github_actions")
                conn.execute(old_action_sql)
                conn.execute("DROP TABLE source_required_actions")
                conn.execute("DROP TABLE source_results")
                conn.execute("DROP TABLE tapd_publication_preparations")
                conn.execute("ALTER TABLE request_events DROP COLUMN source_result_id")
                conn.execute(
                    "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root)"
                    " VALUES('r','org/repo','bot','main','/repo','/trees')"
                )
                conn.execute(
                    "INSERT INTO workstreams(workstream_id,repo_id,created_at,updated_at) VALUES('w','r','now','now')"
                )
                conn.execute(
                    "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at)"
                    " VALUES('t','w','completed','now','now')"
                )
                conn.execute(
                    "INSERT INTO github_actions(action_id,task_id,action_type,target_url,external_id,audit_status,"
                    "publish_status,created_at,metadata_json) VALUES('a','t','comment','https://github.com/org/repo',"
                    "'22','accepted','published','then','{ \"old\": true }')"
                )
                conn.execute(
                    "INSERT INTO publication_intents(publication_id,action_id,idempotency_key,request_fingerprint,status,"
                    "remote_evidence_json,created_at,updated_at)"
                    " VALUES('i','a','k','f','executing','{ \"evidence\": \"old\" }','then','then')"
                )
                rows_before = conn.execute("SELECT * FROM github_actions").fetchall()
                schema_before = conn.execute("SELECT name,sql FROM sqlite_schema ORDER BY name").fetchall()
            with self.assertRaisesRegex(RuntimeError, "reconcile publication intent 'i'"):
                init_database(path)
            with closing(sqlite3.connect(path)) as conn, conn:
                self.assertEqual(
                    conn.execute("SELECT name,sql FROM sqlite_schema ORDER BY name").fetchall(), schema_before
                )
                conn.execute("UPDATE publication_intents SET status='published'")
                intent_before = conn.execute("SELECT * FROM publication_intents").fetchall()
            init_database(path)
            init_database(path)
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(
                    conn.execute("SELECT " + ",".join(action_columns) + " FROM github_actions").fetchall(), rows_before
                )
                self.assertEqual(conn.execute("SELECT * FROM publication_intents").fetchall(), intent_before)
                self.assertEqual(conn.execute("SELECT source_result_id FROM github_actions").fetchall(), [(None,)])
                self.assertTrue(
                    any(column[1] == "source_result_id" for column in conn.execute("PRAGMA table_info(request_events)"))
                )
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                with self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK constraint failed"):
                    conn.execute(
                        "INSERT INTO github_actions(action_id,action_type,created_at) VALUES('orphan','comment','now')"
                    )

    def test_prior_taskless_tapd_owner_upgrade_preserves_required_action_and_receipt(self):
        from robert_agent.persistence.schema.database import init_database

        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "source-feedback.sqlite3"
            init_database(path)
            with closing(sqlite3.connect(path)) as conn, conn:
                current = conn.execute("SELECT sql FROM sqlite_schema WHERE name='github_actions'").fetchone()[0]
                lines = current.splitlines()
                old = "\n".join(
                    "  CHECK (task_id IS NOT NULL OR (result_id IS NULL AND provider = 'tapd' AND action_type = 'comment'))"
                    if "CHECK (task_id IS NOT NULL OR" in line
                    else line
                    for line in lines
                )
                self.assertNotEqual(current, old)
                conn.execute("DROP TABLE github_actions")
                conn.execute(old)
                conn.execute(
                    "INSERT INTO sources(source_id,source_key,source_type,provider,host,project_key,external_id) "
                    "VALUES('s','s','bug','tapd','tapd.example','1','2')"
                )
                conn.execute(
                    "INSERT INTO source_results(source_result_id,source_id,kind,input_version,idempotency_key,"
                    "evidence_json,payload_json,accepted_at) VALUES ('r','s','control_notice','v','k','{}','{}','now')"
                )
                conn.execute(
                    "INSERT INTO github_actions(action_id,source_result_id,provider,action_type,target_json,"
                    "audit_status,publish_status,external_id,created_at) "
                    "VALUES('a','r','tapd','comment','{}','accepted','published','22','now')"
                )
                conn.execute("INSERT INTO source_required_actions VALUES('a','r',0)")
                conn.execute(
                    "INSERT INTO publication_intents(publication_id,action_id,idempotency_key,request_fingerprint,"
                    "status,remote_evidence_json,created_at,updated_at) "
                    "VALUES('i','a','k','f','published','{}','now','now')"
                )
                conn.execute("INSERT INTO tapd_publication_preparations VALUES('i','{}')")
                saved = {
                    table: conn.execute(f"SELECT * FROM {table}").fetchall()
                    for table in (
                        "github_actions",
                        "source_required_actions",
                        "publication_intents",
                        "tapd_publication_preparations",
                    )
                }
            init_database(path)
            init_database(path)
            with closing(sqlite3.connect(path)) as conn, conn:
                for table, before in saved.items():
                    self.assertEqual(conn.execute(f"SELECT * FROM {table}").fetchall(), before)
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                conn.execute(
                    "INSERT INTO github_actions(action_id,source_result_id,provider,action_type,target_json,created_at)"
                    " VALUES('feedback','r','github','source_feedback','{}','now')"
                )
                for provider, kind, target in (
                    ("github", "comment", "{}"),
                    ("github", "source_feedback", None),
                    ("tapd", "source_feedback", "{}"),
                ):
                    with self.subTest(provider=provider, kind=kind, target=target):
                        with self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK constraint failed"):
                            conn.execute(
                                "INSERT INTO github_actions(action_id,source_result_id,provider,action_type,target_json,"
                                "created_at) VALUES('unsupported','r',?,?,?,'now')",
                                (provider, kind, target),
                            )
