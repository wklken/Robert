import json
import unittest
from tests.support import PACKAGE_ROOT
from tests.support import REPO_ROOT

WORKER_MODULE = PACKAGE_ROOT / "worker"


class NewSkillSkeletonTests(unittest.TestCase):
    def test_worker_is_internal_agent_module(self):
        self.assertTrue((WORKER_MODULE / "result.py").exists())
        self.assertTrue((WORKER_MODULE / "snapshot.py").exists())
        self.assertTrue((WORKER_MODULE / "heartbeat.py").exists())
        self.assertTrue((PACKAGE_ROOT / "resources" / "worker-protocol.md").exists())
        self.assertTrue((PACKAGE_ROOT / "resources" / "result-contract.md").exists())
        self.assertTrue(
            (PACKAGE_ROOT / "resources" / "schemas" / "snapshot.schema.json").exists()
        )

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
            PACKAGE_ROOT / "acceptance/readiness.py",
            PACKAGE_ROOT / "operations/initialize.py",
            PACKAGE_ROOT / "resources" / "worker-protocol.md",
            PACKAGE_ROOT / "resources" / "result-contract.md",
            PACKAGE_ROOT / "resources" / "schemas" / "snapshot.schema.json",
        ]
        missing = [
            str(path.relative_to(REPO_ROOT))
            for path in required_paths
            if not path.exists()
        ]
        self.assertEqual(missing, [])

    def test_acceptance_matrix_names_collaboration_workflows(self):
        text = (PACKAGE_ROOT / "resources" / "acceptance-matrix.md").read_text(
            encoding="utf-8"
        )
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
                    if literal == "result.json" and path.relative_to(
                        PACKAGE_ROOT
                    ).as_posix() in {
                        "workflows/review/files.py",
                        "workflows/review/coordinator.py",
                    }:
                        # Native review results are registered artifacts, not the
                        # removed file-backed task/control plane.
                        continue
                    if literal in text:
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT)} contains {literal}"
                        )
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
        schema = json.loads(
            (PACKAGE_ROOT / "resources" / "schemas" / "config.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["required"],
            [
                "version",
                "data_dir",
                "database",
                "github",
                "default_worker",
                "workers",
                "repos",
            ],
        )
        self.assertFalse(schema["properties"]["github"]["additionalProperties"])
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
        self.assertIn(
            "local_poll_seconds", schema["properties"]["scheduler"]["properties"]
        )
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
