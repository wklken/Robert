from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT
WORKER_MODULE = PACKAGE_ROOT / "worker"


class NewSkillSkeletonTests(unittest.TestCase):
    def test_worker_is_internal_agent_module(self):
        self.assertTrue((WORKER_MODULE / "result.py").exists())
        self.assertTrue((WORKER_MODULE / "snapshot.py").exists())
        self.assertTrue((WORKER_MODULE / "heartbeat.py").exists())
        self.assertTrue((PACKAGE_ROOT / "resources" / "worker-protocol.md").exists())
        self.assertTrue((PACKAGE_ROOT / "resources" / "result-contract.md").exists())
        self.assertTrue((PACKAGE_ROOT / "resources" / "schemas" / "snapshot.schema.json").exists())

    def test_package_version_is_public_beta(self):
        from robert_agent import __version__

        self.assertEqual(__version__, "0.1.0b2")

    def test_required_reference_layout_exists(self):
        required_paths = [
            PACKAGE_ROOT / "resources" / "workflow.yml",
            PACKAGE_ROOT / "resources" / "routes.yml",
            PACKAGE_ROOT / "resources" / "classifier.md",
            PACKAGE_ROOT / "resources" / "github-events.md",
            PACKAGE_ROOT / "resources" / "trust-model.md",
            PACKAGE_ROOT / "resources" / "workstream-model.md",
            PACKAGE_ROOT / "resources" / "worker-prompt.md",
            PACKAGE_ROOT / "resources" / "redaction.md",
            PACKAGE_ROOT / "resources" / "failure-modes.md",
            PACKAGE_ROOT / "resources" / "command-matrix.md",
            PACKAGE_ROOT / "resources" / "acceptance-matrix.md",
            PACKAGE_ROOT / "resources" / "kanban-control.md",
            PACKAGE_ROOT / "resources" / "config.example.yml",
            PACKAGE_ROOT / "resources" / "db" / "schema.sql",
            PACKAGE_ROOT / "resources" / "schemas" / "config.schema.json",
            PACKAGE_ROOT / "resources" / "schemas" / "route.schema.json",
            PACKAGE_ROOT / "resources" / "schemas" / "event.schema.json",
            PACKAGE_ROOT / "resources" / "schemas" / "task.schema.json",
            PACKAGE_ROOT / "resources" / "schemas" / "result.schema.json",
            PACKAGE_ROOT / "acceptance.py",
            PACKAGE_ROOT / "init_config.py",
            PACKAGE_ROOT / "resources" / "worker-protocol.md",
            PACKAGE_ROOT / "resources" / "result-contract.md",
            PACKAGE_ROOT / "resources" / "schemas" / "snapshot.schema.json",
        ]
        missing = [str(path.relative_to(REPO_ROOT)) for path in required_paths if not path.exists()]
        self.assertEqual(missing, [])

    def test_acceptance_matrix_names_collaboration_workflows(self):
        text = (PACKAGE_ROOT / "resources" / "acceptance-matrix.md").read_text(encoding="utf-8")
        required_fragments = [
            "issue-assignment-analysis",
            "issue-assignment-new-pr",
            "issue-active-followup-context",
            "dd-created-pr-review-followup",
            "third-party-pr-question",
            "trusted-waiting-user-resume",
            "test_issue_assignment_for_analysis_creates_comment_workflow",
            "test_issue_assignment_bugfix_result_materializes_dd_pr_workflow",
            "test_issue_followup_comment_stays_on_existing_workflow_context",
            "test_dd_pr_followup_routes_to_existing_pr_workflow",
            "test_third_party_pr_question_routes_to_review_comment_workflow",
            "test_trusted_reply_resumes_waiting_for_user_workflow",
            "operator evidence",
            "not yet a live GitHub/worker acceptance test",
        ]
        missing = [fragment for fragment in required_fragments if fragment not in text]
        self.assertEqual(missing, [])

    def test_scripts_do_not_write_legacy_control_plane_files(self):
        forbidden_literals = {"state.json", "task.json", "result.json", "snapshot.json"}
        offenders = []
        for root in [PACKAGE_ROOT]:
            for path in root.rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                for literal in forbidden_literals:
                    if literal == "result.json" and path.relative_to(PACKAGE_ROOT).as_posix() in {
                        "workflows/review/files.py", "workflows/review/coordinator.py"
                    }:
                        # Native review results are registered artifacts, not the
                        # removed file-backed task/control plane.
                        continue
                    if literal in text:
                        offenders.append(f"{path.relative_to(REPO_ROOT)} contains {literal}")
        self.assertEqual(offenders, [])

    def test_result_schemas_require_used_skills(self):
        for path in [PACKAGE_ROOT / "resources" / "schemas" / "result.schema.json"]:
            with self.subTest(path=path.relative_to(REPO_ROOT)):
                schema = json.loads(path.read_text(encoding="utf-8"))
                self.assertIn("used_skills", schema["required"])
                self.assertIn("used_skills", schema["properties"])
                self.assertIn("planned_github_actions", schema["required"])
                self.assertIn("planned_github_actions", schema["properties"])
                self.assertNotIn("actual_github_actions", schema["required"])

    def test_result_schema_allows_no_external_skills(self):
        schema_path = PACKAGE_ROOT / "resources" / "schemas" / "result.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertNotIn("minItems", schema["properties"]["used_skills"])

    def test_config_schema_documents_public_yaml_contract(self):
        schema = json.loads((PACKAGE_ROOT / "resources" / "schemas" / "config.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(
            schema["required"],
            ["version", "data_dir", "database", "github", "default_worker", "workers", "repos"],
        )
        self.assertFalse(
            schema["properties"]["github"]["additionalProperties"]
        )
        worker_config = schema["properties"]["workers"]["additionalProperties"]
        self.assertEqual(
            worker_config["required"],
            ["adapter", "command", "model", "effort"],
        )
        self.assertEqual(
            worker_config["properties"]["fallback_model"],
            {"type": "string", "minLength": 1},
        )
        daemon_config = schema["properties"]["daemon"]
        self.assertIn("local_poll_seconds", schema["properties"]["scheduler"]["properties"])
        self.assertFalse(daemon_config["additionalProperties"])
        repo_config = schema["properties"]["repos"]["items"]
        self.assertEqual(
            repo_config["required"],
            [
                "full_name",
                "checkout",
                "worktrees",
                "default_branch",
                "trusted_actors",
            ],
        )


class ConfigAndSchemaTests(unittest.TestCase):
    expected_tables = {
        "schema_migrations",
        "repos",
        "actors",
        "actor_permissions",
        "github_sources",
        "github_events",
        "workstreams",
        "workstream_sources",
        "work_items",
        "work_item_events",
        "tasks",
        "task_events",
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
        "workflow_outbox",
        "workflow_signal_receipts",
        "workflow_shadow_observations",
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
        from robert_agent.adapters.sqlite import database as storage

        db_path = self.data_dir / "dd.sqlite3"
        first = storage.init_database(db_path)
        second = storage.init_database(db_path)

        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertTrue(db_path.exists())
        self.assertFalse((self.data_dir / "dispatcher.sqlite3").exists())
        with closing(sqlite3.connect(db_path)) as conn, conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
        self.assertTrue(self.expected_tables.issubset(tables))
        self.assertIn("idx_workstreams_one_active_task", indexes)
        self.assertIn("idx_leases_one_active_resource", indexes)
        self.assertIn("idx_wakeups_status_due", indexes)
        self.assertIn("idx_wakeups_result", indexes)

    def test_schema_includes_daemon_tables_and_indexes(self):
        from robert_agent.adapters.sqlite import database as storage

        db_path = self.root / "daemon-schema.sqlite3"
        storage.init_database(db_path)

        with closing(sqlite3.connect(db_path)) as conn, conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }

        self.assertIn("daemon_runs", tables)
        self.assertIn("daemon_events", tables)
        self.assertIn("idx_daemon_events_run_created", indexes)
        self.assertIn("idx_daemon_events_type_created", indexes)

    def test_schema_includes_origin_workstream_link(self):
        from robert_agent.adapters.sqlite import database as storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(workstreams)")
            }
        self.assertIn("origin_workstream_id", columns)

    def test_github_actions_track_publish_status_separately_from_audit(self):
        from robert_agent.adapters.sqlite import database as storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(github_actions)")
            }
        self.assertIn("publish_status", columns)

    def test_schema_includes_remediation_contracts_and_unique_identities(self):
        from robert_agent.adapters.sqlite import database as storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            event_columns = {
                row[1]: row[4]
                for row in conn.execute("PRAGMA table_info(github_events)")
            }
            task_columns = {
                row[1]: row[4]
                for row in conn.execute("PRAGMA table_info(tasks)")
            }
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
        from robert_agent.adapters.sqlite import database as storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
            entry_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(project_memory_entries)")
            }
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
        from robert_agent.adapters.sqlite import database as storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
            candidate_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(knowledge_candidates)")
            }
            runtime_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(runtime_knowledge)")
            }
        self.assertTrue({"knowledge_candidates", "runtime_knowledge"}.issubset(tables))
        self.assertIn("idx_knowledge_candidates_status", indexes)
        self.assertIn("idx_runtime_knowledge_lookup", indexes)
        self.assertIn("status", candidate_columns)
        self.assertIn("scope_type", runtime_columns)
        self.assertIn("retrieval_boost_json", runtime_columns)


if __name__ == "__main__":
    unittest.main()
