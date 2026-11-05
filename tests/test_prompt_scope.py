import json
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import render_prompt, route


class PromptScopeTests(unittest.TestCase):
    def render(self, route_id, **kwargs):
        return render_prompt.render_prompt(
            {"task_id": "task-1", "attempt_id": "attempt-1", "workstream_id": "github:x/y!1"},
            route.ROUTES[route_id],
            [{"event_fingerprint": "comment:1", "event_type": "comment"}],
            runtime_context={
                "github_context": {"md_path": "/context/input.md", "json_path": "/context/input.json"},
                "result_script": "/runtime/result.py", "status_script": "/runtime/status.py",
            },
            **kwargs,
        )

    def test_mutation_testing_requires_explicit_task_or_repository_requirement(self):
        for route_id in ("new-pr", "update-existing-pr"):
            with self.subTest(route_id=route_id):
                prompt = self.render(route_id)
                self.assertIn("Only run mutation testing when explicitly requested by the task or required by applicable repository instructions", prompt)
                self.assertIn("Otherwise omit mutation testing without a skip explanation", prompt)
                self.assertNotIn("When feasible, use scoped mutation testing", prompt)

    def test_classification_has_only_its_result_fields_and_no_publication_help(self):
        prompt = self.render("classification-result")
        payload = json.loads(prompt.split("<<'JSON'\n", 1)[1].split("\nJSON", 1)[0])
        self.assertIn("recommended_route", payload)
        self.assertIn("branch_slug", payload)
        self.assertNotIn("review_point_evaluation", payload)
        self.assertNotIn("memory_delta", payload)
        self.assertNotIn("robert-comment", prompt)
        self.assertNotIn("robert-workstream", prompt)
        self.assertNotIn("When publishing a PR body", prompt)
        self.assertNotIn("Relevant Project Memories", prompt)
        self.assertNotIn("Approved Runtime Knowledge", prompt)
        self.assertNotIn("run latest", prompt)

    def test_code_result_omits_classification_fields(self):
        prompt = self.render("new-pr")
        payload = json.loads(prompt.split("<<'JSON'\n", 1)[1].split("\nJSON", 1)[0])
        self.assertNotIn("recommended_route", payload)
        self.assertNotIn("branch_slug", payload)
        self.assertNotIn("review_point_evaluation", payload)
        self.assertIn("verification", payload)
        self.assertIn("planned_github_actions", payload)

    def test_context_has_one_primary_entry_and_json_is_on_demand(self):
        prompt = self.render("new-pr")
        self.assertIn("Read /context/input.md first", prompt)
        self.assertIn("Use /context/input.json only when structured fields are needed", prompt)
        self.assertNotIn("Read those files before acting", prompt)

    def test_nonempty_memory_keeps_evidence_requirements(self):
        prompt = self.render("new-pr", project_memories=[{"memory_id": "m1", "short_summary": "Check callers"}])
        self.assertIn("Relevant Project Memories", prompt)
        self.assertIn('"memory_id": "m1"', prompt)
        self.assertIn("used_memory_ids", prompt)

    def test_review_policy_is_scoped_to_review_workflows(self):
        for route_id in ("review-pr", "review-comment", "update-existing-pr"):
            with self.subTest(route_id=route_id):
                prompt = self.render(route_id)
                self.assertIn("Treat every review round as a fresh independent judgment", prompt)
                self.assertIn("Security review is required and proportional", prompt)
                self.assertIn("A blocking finding needs", prompt)
                self.assertIn("stop repeating the review", prompt)
                self.assertIn("@wklken", prompt)
        for route_id in ("classification-result", "new-pr", "comment-analysis"):
            with self.subTest(route_id=route_id):
                self.assertNotIn("Review judgment policy:", self.render(route_id))

    def test_review_escalation_has_configured_identity_and_uses_planned_comment(self):
        prompt = render_prompt.render_prompt(
            {"task_id": "task-1", "attempt_id": "attempt-1", "workstream_id": "github:x/y!1"},
            route.ROUTES["review-pr"],
            [{"source_type": "pull_request", "pr_author_login": "Robert-Bot"}],
            runtime_context={"github_account": "robert-bot"},
        )
        self.assertIn('Configured Robert GitHub account: "robert-bot"', prompt)
        self.assertIn("case-insensitively", prompt)
        self.assertIn("planned comment body", prompt)
        self.assertIn("Robert adds the trusted PR author mention", prompt)

    def test_review_policy_preserves_recovery_evidence_and_feedback_scope(self):
        prompt = self.render("review-comment")
        self.assertIn("Reuse still-valid edits and verification evidence on recovery", prompt)
        self.assertIn("do not reuse a verdict as proof", prompt)
        self.assertIn("The policy below applies only when evaluating review feedback", prompt)
        self.assertIn("Do not expand supplied-feedback verification into a fresh source review", prompt)
        self.assertIn("Do not modify source or plan a push", prompt)


if __name__ == "__main__":
    unittest.main()
