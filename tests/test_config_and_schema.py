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

        self.assertEqual(__version__, "0.1.0b1")

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
            ["version", "github", "workers", "repos"],
        )
        self.assertFalse(
            schema["properties"]["github"]["additionalProperties"]
        )
        worker_config = schema["properties"]["workers"]["additionalProperties"]
        self.assertEqual(
            worker_config["required"],
            ["adapter", "command", "model", "effort"],
        )
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

    def write_config(self, trusted_actors=True):
        actors = "\n      - wklken" if trusted_actors else ""
        path = self.root / "config.yml"
        path.write_text(
            f"""data_dir: {self.data_dir}
database: dd.sqlite3
max_concurrency: 3
stale_after_minutes: 20
hard_timeout_minutes: 90
default_max_retries: 0
repos:
  - full_name: example/backend
    github_account: robert-bot
    trusted_actors:{actors}
    default_base_branch: master
    repo_root: {self.repo_root}
    worktree_root: {self.worktree_root}
""",
            encoding="utf-8",
        )
        return path

    def test_config_with_one_repo_validates(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["max_concurrency"], 3)

    def test_config_defaults_max_concurrency_to_three(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "max_concurrency: 3\n",
                "",
            ),
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["max_concurrency"], 3)
        self.assertEqual(result["db_path"], str(self.data_dir / "dd.sqlite3"))
        self.assertEqual(result["repos"][0]["trusted_actors"], ["wklken"])
        self.assertEqual(result["worker_startup_grace_seconds"], 300)
        self.assertEqual(result["python_bin"], "python3")
        self.assertEqual(result["default_worker"]["name"], "default")
        self.assertEqual(result["route_worker_models"], {})
        self.assertEqual(result["repos"][0]["max_concurrency"], 3)

    def test_repo_max_concurrency_is_bounded_by_global_capacity(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "    trusted_actors:\n",
                "    max_concurrency: 2\n    trusted_actors:\n",
            ),
            encoding="utf-8",
        )
        valid = validate_config.validate_config(config_path, skip_external=True)
        self.assertTrue(valid["ok"], valid)
        self.assertEqual(valid["repos"][0]["max_concurrency"], 2)

        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "    max_concurrency: 2", "    max_concurrency: 4"
            ),
            encoding="utf-8",
        )
        too_large = validate_config.validate_config(config_path, skip_external=True)
        self.assertFalse(too_large["ok"])
        self.assertIn("repos[0].max_concurrency", too_large["safe_error"])

    def test_config_defaults_daemon_settings(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["daemon"],
            {
                "enabled": True,
                "local_poll_seconds": 5,
                "github_poll_seconds": 300,
                "github_poll_when_full_seconds": 600,
                "rate_limit_cache_seconds": 300,
                "min_search_remaining": 10,
                "min_core_remaining": 500,
                "live_run_timeout_seconds": 300,
                "local_drain_timeout_seconds": 180,
                "event_retention_days": 7,
                "run_on_start": False,
            },
        )

    def test_config_accepts_python_bin(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\npython_bin: /usr/bin/python3.11",
            ),
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["python_bin"], "/usr/bin/python3.11")

    def test_config_accepts_worker_startup_grace_seconds(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "hard_timeout_minutes: 90",
                "hard_timeout_minutes: 90\nworker_startup_grace_seconds: 45",
            ),
            encoding="utf-8",
        )
        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["worker_startup_grace_seconds"], 45)

    def test_config_accepts_worker_command(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\nworker_command: custom-cbc",
            ),
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["worker_command"], "custom-cbc")
        self.assertEqual(result["worker_agent"], "cbc")
        self.assertEqual(result["worker_agent_config"]["agent"], "cbc")
        self.assertEqual(result["worker_agent_config"]["command"], "custom-cbc")

    def test_config_accepts_codex_worker_agent(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\nworker_agent: codex\nworker_command: /opt/homebrew/bin/codex",
            ),
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["worker_agent"], "codex")
        self.assertEqual(result["worker_command"], "/opt/homebrew/bin/codex")
        self.assertEqual(
            result["worker_agent_config"],
            {
                "agent": "codex",
                "command": "/opt/homebrew/bin/codex",
                "command_argv": ["/opt/homebrew/bin/codex"],
            },
        )

    def test_config_accepts_named_workers_and_resolves_route_defaults(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\n"
                "workers:\n"
                "  - name: default\n"
                "    agent: cbc\n"
                "    command: cbc\n"
                "    default_model: gpt-5.4\n"
                "    default_effort: high\n"
                "  - name: reviewer\n"
                "    agent: codex\n"
                "    command: /opt/homebrew/bin/codex\n"
                "    default_model: gpt-5.6-sol\n"
                "    default_effort: xhigh\n"
                "route_worker_models:\n"
                "  review-pr:\n"
                "    worker: reviewer\n"
                "  review-comment:\n"
                "    worker: reviewer\n"
                "    effort: medium\n"
                "  classification-result:\n"
                "    worker: default\n"
                "    model: gpt-5.6-terra",
            ),
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["workers"],
            [
                {
                    "name": "default",
                    "agent": "cbc",
                    "command": "cbc",
                    "command_argv": ["cbc"],
                    "prompt_transport": "stdin",
                    "timeout_seconds": 5400,
                    "environment_allowlist": [],
                    "default_model": "gpt-5.4",
                    "default_effort": "high",
                },
                {
                    "name": "reviewer",
                    "agent": "codex",
                    "command": "/opt/homebrew/bin/codex",
                    "command_argv": ["/opt/homebrew/bin/codex"],
                    "prompt_transport": "stdin",
                    "timeout_seconds": 5400,
                    "environment_allowlist": [],
                    "default_model": "gpt-5.6-sol",
                    "default_effort": "xhigh",
                },
            ],
        )
        self.assertEqual(result["default_worker"]["name"], "default")
        self.assertEqual(
            result["route_worker_models"],
            {
                "review-pr": {
                    "worker": "reviewer",
                    "model": "gpt-5.6-sol",
                    "effort": "xhigh",
                },
                "review-comment": {
                    "worker": "reviewer",
                    "model": "gpt-5.6-sol",
                    "effort": "medium",
                },
                "classification-result": {
                    "worker": "default",
                    "model": "gpt-5.6-terra",
                    "effort": "high",
                },
            },
        )

    def test_config_rejects_duplicate_worker_names(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\n"
                "workers:\n"
                "  - name: reviewer\n"
                "    agent: codex\n"
                "    command: codex\n"
                "    default_model: gpt-5.6-sol\n"
                "    default_effort: high\n"
                "  - name: reviewer\n"
                "    agent: tcodex\n"
                "    command: tcodex\n"
                "    default_model: gpt-5.6-sol\n"
                "    default_effort: high",
            ),
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["safe_error"], "invalid config: duplicate worker name: reviewer")

    def test_config_rejects_unknown_route_worker(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\n"
                "workers:\n"
                "  - name: default\n"
                "    agent: cbc\n"
                "    command: cbc\n"
                "    default_model: gpt-5.4\n"
                "    default_effort: high\n"
                "route_worker_models:\n"
                "  review-pr:\n"
                "    worker: reviewer",
            ),
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["safe_error"],
            "invalid config: route_worker_models.review-pr references unknown worker: reviewer",
        )

    def test_config_rejects_named_worker_without_command(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\n"
                "workers:\n"
                "  - name: reviewer\n"
                "    agent: codex\n"
                "    default_model: gpt-5.6-sol\n"
                "    default_effort: high",
            ),
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["safe_error"],
            "invalid config: workers[0].command must not be empty",
        )

    def test_config_rejects_unknown_worker_agent(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\nworker_agent: claude",
            ),
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_config")
        self.assertEqual(result["safe_error"], "unsupported worker_agent: claude")

    def test_config_accepts_route_worker_models(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\n"
                "route_worker_models:\n"
                "  classification-result:\n"
                "    model: gpt-5.6-sol\n"
                "    effort: high",
            ),
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["route_worker_models"],
            {
                "classification-result": {
                    "worker": "default",
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                }
            },
        )

    def test_config_route_model_override_inherits_default_effort(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\n"
                "route_worker_models:\n"
                "  classification-result:\n"
                "    model: gpt-5.6-sol",
            ),
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertEqual(
            result["route_worker_models"]["classification-result"],
            {
                "worker": "default",
                "model": "gpt-5.6-sol",
                "effort": "high",
            },
        )

    def test_config_rejects_scalar_route_worker_model(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\nroute_worker_models:\n  classification-result: gpt-5.6-sol",
            ),
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_config")
        self.assertEqual(
            result["safe_error"],
            "invalid config: route_worker_models.classification-result must be a worker configuration",
        )

    def test_config_rejects_data_dir_file(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        data_file = self.root / "data-file"
        data_file.write_text("not a directory", encoding="utf-8")
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                f"data_dir: {self.data_dir}",
                f"data_dir: {data_file}",
            ),
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_config")
        self.assertIn("data_dir is not a directory", result["safe_error"])
        self.assertIn(str(data_file), result["safe_error"])

    def test_config_rejects_empty_database_name(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database:",
            ),
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_config")
        self.assertEqual(result["safe_error"], "database must not be empty")

    def test_config_rejects_database_path(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: nested/dd.sqlite3",
            ),
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_config")
        self.assertEqual(
            result["safe_error"],
            "database must be a filename, not a path: nested/dd.sqlite3",
        )

    def test_config_rejects_empty_python_bin(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\npython_bin:",
            ),
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_config")
        self.assertEqual(result["safe_error"], "python_bin must not be empty")

    def test_config_rejects_nonpositive_max_concurrency(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "max_concurrency: 3",
                "max_concurrency: 0",
            ),
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_config")
        self.assertEqual(result["safe_error"], "max_concurrency must be at least 1")

    def test_config_rejects_nonpositive_runtime_thresholds(self):
        from robert_agent import validate_config
        field_defaults = {
            "stale_after_minutes": "20",
            "hard_timeout_minutes": "90",
            "worker_startup_grace_seconds": None,
            "lease_ttl_minutes": None,
        }
        for field, default in field_defaults.items():
            with self.subTest(field=field):
                config_path = self.write_config()
                config_text = config_path.read_text(encoding="utf-8")
                if default is None:
                    config_path.write_text(
                        config_text.replace(
                            "hard_timeout_minutes: 90",
                            f"hard_timeout_minutes: 90\n{field}: 0",
                        ),
                        encoding="utf-8",
                    )
                else:
                    config_path.write_text(
                        config_text.replace(f"{field}: {default}", f"{field}: 0"),
                        encoding="utf-8",
                    )

                result = validate_config.validate_config(config_path, skip_external=True)

                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "failed_config")
                self.assertEqual(result["safe_error"], f"{field} must be at least 1")

    def test_config_rejects_nonpositive_daemon_setting(self):
        from robert_agent import validate_config
        good_config_path = self.write_config()
        config_path = self.root / "bad-daemon-config.yml"
        text = good_config_path.read_text(encoding="utf-8")
        config_path.write_text(
            text + "\ndaemon_local_poll_seconds: 0\n",
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_config")
        self.assertIn("daemon_local_poll_seconds must be at least 1", result["safe_error"])

    def test_config_without_trusted_actors_fails(self):
        from robert_agent import validate_config
        config_path = self.write_config(trusted_actors=False)
        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_config")
        self.assertIn("trusted_actors", result["safe_error"])

    def test_config_template_is_an_unconfigured_public_skeleton(self):
        from robert_agent import validate_config
        config_path = self.root / "config.yml"
        config_path.write_text(
            (PACKAGE_ROOT / "resources" / "config.example.yml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_config")
        self.assertIn("repos must contain at least one repository", result["safe_error"])

    def test_config_invalid_repo_root_reports_configured_path(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        bad_repo_root = self.root / "missing-repo"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                f"repo_root: {self.repo_root}",
                f"repo_root: {bad_repo_root}",
            ),
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_config")
        self.assertIn("repo_root is not a git checkout", result["safe_error"])
        self.assertIn(str(bad_repo_root), result["safe_error"])

    def test_config_with_multiple_repos_validates(self):
        from robert_agent import validate_config
        other_repo = self.root / "other-repo"
        other_repo.mkdir()
        (other_repo / ".git").mkdir()
        other_worktree = other_repo / ".worktrees"
        other_worktree.mkdir()
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + f"""  - full_name: Example/other
    github_account: robot-other
    trusted_actors:
      - other-maintainer
    default_base_branch: main
    repo_root: {other_repo}
    worktree_root: {other_worktree}
""",
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual([repo["full_name"] for repo in result["repos"]], [
            "example/backend",
            "Example/other",
        ])
        self.assertEqual(result["repos"][0]["github_account"], "robert-bot")
        self.assertEqual(result["repos"][1]["github_account"], "robot-other")

    def test_config_global_github_account_is_inherited_by_repo(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        text = config_path.read_text(encoding="utf-8")
        text = text.replace("database: dd.sqlite3\n", "database: dd.sqlite3\ngithub_account: robot-global\n")
        text = text.replace("    github_account: robert-bot\n", "")
        config_path.write_text(text, encoding="utf-8")

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["github_account"], "robot-global")
        self.assertEqual(result["repos"][0]["github_account"], "robot-global")

    def test_config_rejects_duplicate_repo_full_name(self):
        from robert_agent import validate_config
        other_repo = self.root / "other-repo"
        other_repo.mkdir()
        (other_repo / ".git").mkdir()
        other_worktree = other_repo / ".worktrees"
        other_worktree.mkdir()
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + f"""  - full_name: example/backend
    github_account: robot-other
    trusted_actors:
      - other-maintainer
    default_base_branch: main
    repo_root: {other_repo}
    worktree_root: {other_worktree}
""",
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_config")
        self.assertIn("duplicate repo full_name", result["safe_error"])

    def test_config_rejects_missing_effective_github_account(self):
        from robert_agent import validate_config
        config_path = self.write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace("    github_account: robert-bot\n", ""),
            encoding="utf-8",
        )

        result = validate_config.validate_config(config_path, skip_external=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_config")
        self.assertIn("github_account", result["safe_error"])

    def test_fresh_database_init_creates_expected_tables_and_is_idempotent(self):
        from robert_agent import storage

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
        from robert_agent import storage

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
        from robert_agent import storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(workstreams)")
            }
        self.assertIn("origin_workstream_id", columns)

    def test_github_actions_track_publish_status_separately_from_audit(self):
        from robert_agent import storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(github_actions)")
            }
        self.assertIn("publish_status", columns)

    def test_schema_includes_project_memory_tables_and_lookup_index(self):
        from robert_agent import storage

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
        from robert_agent import storage

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
