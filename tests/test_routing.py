import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class RoutingTests(unittest.TestCase):
    def test_routes_do_not_advertise_internal_worker_as_skill(self):
        from robert_agent import route
        internal_worker_name = "robert-worker"
        for route_id, route_config in route.ROUTES.items():
            with self.subTest(route_id=route_id):
                self.assertNotIn(internal_worker_name, route_config["recommended_skills"])

    def test_web_analysis_routes_to_local_result(self):
        from robert_agent import route
        result = route.route_task({"origin_type": "web", "intent": "analysis"})

        self.assertEqual(result["route_id"], "local-result")
        self.assertEqual(result["expected_output"], "local_result")
        self.assertEqual(result["allowed_github_actions"], [])
        self.assertFalse(result["needs_worktree"])

    def test_web_bug_fix_still_routes_to_new_pr(self):
        from robert_agent import route
        result = route.route_task({"origin_type": "web", "intent": "bug_fix"})

        self.assertEqual(result["route_id"], "new-pr")

    def test_low_confidence_route_creates_classification_task(self):
        from robert_agent import route
        result = route.route_task({"intent": "unclear"})

        self.assertEqual(result["expected_output"], "classification_result")
        self.assertEqual(result["allowed_github_actions"], [])

    def test_analysis_route_cannot_open_pr(self):
        from robert_agent import route
        result = route.route_task({"intent": "analysis"})

        self.assertEqual(result["expected_output"], "comment_analysis")
        self.assertNotIn("open_pr", result["allowed_github_actions"])
        self.assertEqual(
            result["recommended_skills"],
            ["fast-code-path", "fast-zoom-out", "fast-verify-review-point"],
        )

    def test_new_pr_route_only_advertises_publisher_supported_actions(self):
        from robert_agent import route
        result = route.route_task({"intent": "bug_fix"})

        self.assertEqual(result["expected_output"], "new_pr")
        self.assertEqual(
            result["allowed_github_actions"],
            ["push_existing_pr", "open_pr", "comment"],
        )
        self.assertEqual(
            result["recommended_skills"],
            [
                "fast-small-pr",
                "fast-code-path",
                "fast-add-tests",
                "fast-test-fix",
                "fast-preflight",
            ],
        )

    def test_update_existing_pr_route_cannot_open_new_pr(self):
        from robert_agent import route
        result = route.route_task({"intent": "pr_followup_fix", "has_open_dd_pr": True})

        self.assertEqual(result["expected_output"], "update_existing_pr")
        self.assertIn("push_existing_pr", result["allowed_github_actions"])
        self.assertNotIn("open_pr", result["allowed_github_actions"])
        self.assertEqual(result["required_skills"], [])
        self.assertEqual(
            result["recommended_skills"],
            [
                "fast-small-pr",
                "fast-verify-review-point",
                "fast-code-path",
                "fast-add-tests",
                "fast-test-fix",
                "fast-code-simplify",
                "fast-preflight",
            ],
        )

    def test_dd_pr_bug_fix_followup_updates_existing_pr(self):
        from robert_agent import route
        result = route.route_task(
            {
                "intent": "bug_fix",
                "source_type": "pull_request",
                "has_open_dd_pr": True,
            }
        )

        self.assertEqual(result["expected_output"], "update_existing_pr")
        self.assertIn("push_existing_pr", result["allowed_github_actions"])
        self.assertNotIn("open_pr", result["allowed_github_actions"])

    def test_third_party_pr_bug_fix_request_stays_comment_only(self):
        from robert_agent import route
        result = route.route_task(
            {
                "intent": "bug_fix",
                "source_type": "pull_request",
                "has_open_dd_pr": False,
            }
        )

        self.assertEqual(result["expected_output"], "review_comment")
        self.assertEqual(result["allowed_github_actions"], ["comment"])
        self.assertFalse(result["needs_worktree"])
        self.assertEqual(result["required_skills"], [])
        self.assertEqual(
            result["recommended_skills"],
            [
                "fast-review-github-pr",
                "fast-verify-review-point",
                "fast-code-path",
            ],
        )

    def test_review_request_routes_to_source_review_worktree(self):
        from robert_agent import route
        result = route.route_task(
            {
                "event_type": "review_request",
                "intent": "review_request",
                "source_type": "pull_request",
                "requested_reviewer": "robert-bot",
            }
        )

        self.assertEqual(result["route_id"], "review-pr")
        self.assertEqual(result["expected_output"], "pr_review_comment")
        self.assertEqual(result["allowed_github_actions"], ["comment"])
        self.assertTrue(result["needs_worktree"])
        self.assertEqual(result["worktree_mode"], "review_pr")
        self.assertEqual(result["required_skills"], [])

    def test_dd_pr_unclear_followup_defaults_to_existing_pr_update(self):
        from robert_agent import route
        result = route.route_task(
            {
                "source_type": "pull_request",
                "has_open_dd_pr": True,
                "intent": "unclear",
            }
        )

        self.assertEqual(result["expected_output"], "update_existing_pr")
        self.assertIn("push_existing_pr", result["allowed_github_actions"])
        self.assertNotIn("open_pr", result["allowed_github_actions"])

    def test_prompt_includes_metadata_and_allowed_actions(self):
        from robert_agent import render_prompt
        from robert_agent import route
        route_result = route.route_task({"intent": "bug_fix"})
        prompt = render_prompt.render_prompt(
            task={
                "task_id": "task-1",
                "attempt_id": "attempt-1",
                "workstream_id": "github:example/backend#123",
            },
            route_result=route_result,
            events=[{"event_fingerprint": "comment:1"}],
            runtime_context={
                "db_path": "/tmp/dd.sqlite3",
                "result_script": "/agent/result.py",
                "snapshot_script": "/worker/snapshot.py",
                "heartbeat_script": "/worker/heartbeat.py",
                "status_script": "/agent/status.py",
            },
        )

        self.assertIn("robert-workstream", prompt)
        self.assertIn("allowed_github_actions", prompt)
        self.assertIn("push_existing_pr", prompt)
        self.assertIn("open_pr", prompt)
        self.assertIn("must not create, modify, or install skills", prompt)
        self.assertIn("recommended_skills", prompt)
        self.assertNotIn("allowed_skills", prompt)
        self.assertIn("/worker/snapshot.py", prompt)
        self.assertIn("/worker/heartbeat.py", prompt)
        self.assertIn("python3 /worker/snapshot.py", prompt)
        self.assertIn("python3 /worker/heartbeat.py", prompt)
        self.assertIn("python3 /agent/status.py --db /tmp/dd.sqlite3 status", prompt)
        self.assertIn("python3 /agent/status.py --db /tmp/dd.sqlite3 source <source_key-or-number-or-url>", prompt)
        self.assertIn("ad hoc SQLite queries", prompt)
        self.assertIn('non-empty "type"', prompt)
        self.assertIn('"planned_github_actions"', prompt)
        self.assertNotIn('"actual_github_actions"', prompt)
        self.assertIn("open_pr requires repo, head, base, title, and body", prompt)
        self.assertIn("push_existing_pr requires worktree_path and branch", prompt)
        self.assertIn("idempotency markers", prompt)
        self.assertIn('"used_skills"', prompt)
        self.assertIn("recommended_skills are guidance", prompt)
        self.assertIn("may use any installed local skill", prompt)
        self.assertIn('"used_skills": []', prompt)
        self.assertNotIn('"used_skills": ["fast-small-pr"]', prompt)

    def test_update_existing_pr_prompt_requires_review_point_evaluation(self):
        from robert_agent import render_prompt
        from robert_agent import route
        route_result = route.route_task(
            {"intent": "bug_fix", "source_type": "pull_request", "has_open_dd_pr": True}
        )
        prompt = render_prompt.render_prompt(
            task={
                "task_id": "task-1",
                "attempt_id": "attempt-1",
                "workstream_id": "github:example/repo!77",
            },
            route_result=route_result,
            events=[{"event_fingerprint": "comment:77"}],
            runtime_context={
                "db_path": "/tmp/dd.sqlite3",
                "result_script": "/agent/result.py",
            },
        )

        self.assertIn("required_skills: []", prompt)
        self.assertIn("Review-point evaluation", prompt)
        self.assertIn("Every review report requires a public comment response", prompt)
        self.assertIn("<review report response>", prompt)
        self.assertIn('"type": "comment"', prompt)
        self.assertIn('"target_url": "<target GitHub URL>"', prompt)
        self.assertIn('"review_point_evaluation": []', prompt)
        self.assertIn('"used_skills": []', prompt)

    def test_comment_analysis_prompt_result_example_includes_required_comment_action(self):
        from robert_agent import render_prompt
        from robert_agent import route
        route_result = route.route_task({"intent": "analysis"})
        prompt = render_prompt.render_prompt(
            task={
                "task_id": "task-1",
                "attempt_id": "attempt-1",
                "workstream_id": "github:example/repo#77",
            },
            route_result=route_result,
            events=[
                {
                    "event_fingerprint": "comment:77",
                    "url": "https://github.com/example/repo/issues/77",
                }
            ],
            runtime_context={
                "db_path": "/tmp/dd.sqlite3",
                "result_script": "/agent/result.py",
            },
        )

        self.assertIn("--output-type comment_analysis", prompt)
        self.assertIn('"type": "comment"', prompt)
        self.assertIn('"target_url": "https://github.com/example/repo/issues/77"', prompt)
        self.assertIn("<!-- robert-comment task_id=task-1 attempt_id=attempt-1", prompt)
        self.assertNotIn('"planned_github_actions": []', prompt)

    def test_classification_prompt_limits_worker_scope(self):
        from robert_agent import render_prompt
        from robert_agent import route
        route_result = route.route_task({"intent": "unclear"})
        prompt = render_prompt.render_prompt(
            task={
                "task_id": "task-1",
                "attempt_id": "attempt-1",
                "workstream_id": "github:example/repo#77",
            },
            route_result=route_result,
            events=[
                {
                    "event_fingerprint": "comment:77",
                    "title": "Add frontend env var",
                    "body": "Need a jump link.",
                }
            ],
            runtime_context={
                "db_path": "/tmp/dd.sqlite3",
                "result_script": "/agent/result.py",
            },
        )

        self.assertIn("Classification scope:", prompt)
        self.assertIn("only for task classification", prompt)
        self.assertIn("not the final implementation or execution worker", prompt)
        self.assertIn("changed-file or module list", prompt)
        self.assertIn("Avoid broad repo exploration", prompt)
        self.assertIn("--output-type classification_result", prompt)
        self.assertIn('"branch_slug": ""', prompt)
        self.assertIn("meaningful lowercase English kebab-case", prompt)

    def test_new_pr_prompt_includes_origin_workstream_metadata(self):
        from robert_agent import render_prompt
        from robert_agent import route
        route_result = route.route_task({"intent": "bug_fix"})
        prompt = render_prompt.render_prompt(
            task={
                "task_id": "task-1",
                "attempt_id": "attempt-1",
                "workstream_id": "github:example/backend#123",
            },
            route_result=route_result,
            events=[{"event_fingerprint": "comment:1"}],
            runtime_context={
                "db_path": "/tmp/dd.sqlite3",
                "result_script": "/agent/result.py",
                "snapshot_script": "/worker/snapshot.py",
                "heartbeat_script": "/worker/heartbeat.py",
            },
        )

        self.assertIn("origin_workstream_id: github:example/backend#123", prompt)
        self.assertIn("source_issue: 123", prompt)
        self.assertIn("Refs #123", prompt)
        self.assertIn("must visibly reference #123 outside the hidden metadata", prompt)
        self.assertIn("Use `Fixes #123` or `Closes #123` only when", prompt)
        self.assertIn("comment-triggered for only one item", prompt)
        self.assertIn('"type": "push_existing_pr"', prompt)
        self.assertNotIn("Classification scope:", prompt)


if __name__ == "__main__":
    unittest.main()
