from robert_agent.context.worker_prompt import worker_prompt_input
import json
import unittest
from dataclasses import replace

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.context import prompts as render_prompt
from robert_agent.context.prompt_contract import ContextItem, OutputContract, PromptInput
from robert_agent.context.native_prompts import review_evaluate_input
from robert_agent.workflows.review import files as review_files
from robert_agent.workflows import routes as route


class PromptScopeTests(unittest.TestCase):
    def test_code_verification_reuses_only_matching_input_and_hook_evidence(self):
        for route_id in ("new-pr", "update-existing-pr"):
            with self.subTest(route_id=route_id):
                prompt = self.render(route_id)
                for requirement in (
                    "source content, tests, dependency lockfiles, configuration, command and runtime",
                    "repository-required checks",
                    "commit hooks",
                    "If a hook changes files",
                    "Do not disable or bypass hooks",
                    "Retain the original command, exit status and evidence location",
                ):
                    self.assertIn(requirement, prompt)

    def test_review_template_versions_use_fixed_resources_and_reject_unknown_versions(self):
        manifest = {
            "schema_version": 1,
            "round_id": "round-1",
            "snapshot_id": "snapshot-1",
            "head_sha": "a" * 40,
            "context_dir": "/context",
            "worktree_path": "/tree",
            "artifact_root": "/artifacts",
        }
        request = review_evaluate_input(
            manifest, severities=review_files.SEVERITIES, recommendations=review_files.RECOMMENDATIONS
        )
        current = render_prompt.render_prompt(request)
        self.assertEqual(render_prompt.render_prompt(request, stage_version=3).text, current.text)
        self.assertIn("Do not search unrelated checkouts", current.text)
        self.assertIn("could change a finding or the merge recommendation", current.text)
        previous = render_prompt.render_prompt(request, stage_version=2)
        self.assertIn("executable verification is still required", previous.text)
        self.assertNotIn("Do not search unrelated checkouts", previous.text)
        legacy = render_prompt.render_prompt(request, stage_version=1)
        self.assertIn("preparation summary's documented interpreter", legacy.text)
        self.assertNotIn("discovery inventories", legacy.text)
        self.assertIn("stages/review-evaluate-model-preparation-v1.md", legacy.metadata["resources"])
        for invalid in (0, 4, True, "../review-boundary.md"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "unsupported prompt stage version"):
                render_prompt.render_prompt(request, stage_version=invalid)

    def render(self, route_id, **kwargs):
        return render_prompt.render_prompt(
            worker_prompt_input(
                {
                    "task_id": "task-1",
                    "attempt_id": "attempt-1",
                    "workstream_id": "github:x/y!1",
                },
                route.ROUTES[route_id],
                [{"event_fingerprint": "comment:1", "event_type": "comment"}],
                runtime_context={
                    "github_context": {
                        "md_path": "/context/input.md",
                        "json_path": "/context/input.json",
                    },
                    "result_script": "/runtime/result.py",
                    "status_script": "/runtime/status.py",
                    **kwargs.pop("runtime_context", {}),
                },
                **kwargs,
            )
        ).text

    def test_mutation_testing_requires_explicit_task_or_repository_requirement(self):
        for route_id in ("new-pr", "update-existing-pr"):
            with self.subTest(route_id=route_id):
                prompt = self.render(route_id)
                self.assertIn(
                    "Only run mutation testing when explicitly requested by the task or required by applicable repository instructions",
                    prompt,
                )
                self.assertIn("Otherwise omit mutation testing without a skip explanation", prompt)
                self.assertNotIn("When feasible, use scoped mutation testing", prompt)

    def test_delivery_prompt_requests_a_separately_written_receipt_summary(self):
        prompt = self.render("new-pr")
        self.assertIn('"receipt_summary":', prompt)
        self.assertIn("one or two short sentences", prompt)
        self.assertIn("what changed or what you concluded", prompt)
        self.assertNotIn('"receipt_summary":', self.render("classification-result"))

    def test_task_prompt_exposes_ordered_sections_and_stage_identity(self):
        prompt = self.render("new-pr")
        headings = [line for line in prompt.splitlines() if line.startswith("## ")]
        self.assertEqual(
            headings,
            [
                "## Stage",
                "## Task",
                "## Boundaries",
                "## Context",
                "## Work",
                "## Verification",
                "## Delivery",
                "## Ending",
            ],
        )
        self.assertIn("task.implement", prompt.split("## Task", 1)[0])

    def test_renderer_rejects_stage_protocol_and_context_mismatches(self):
        request = PromptInput(
            "supervision.diagnose",
            {"bundle_id": "bundle-1"},
            OutputContract("stdout-json", "diagnosis", "stdout", "Robert supervisor"),
        )
        with self.assertRaisesRegex(ValueError, "invalid output contract"):
            render_prompt.render_prompt(replace(request, output=replace(request.output, schema="decision")))
        with self.assertRaisesRegex(ValueError, "file context requires file access"):
            render_prompt.render_prompt(
                replace(
                    request,
                    context_items=(ContextItem("logs", "Failure logs", "Worker", delivery="file", locator="/logs"),),
                )
            )
        with self.assertRaisesRegex(ValueError, "required context is unavailable"):
            render_prompt.render_prompt(
                replace(
                    request,
                    context_items=(
                        ContextItem(
                            "request",
                            "Current request",
                            "Original task",
                            requirement="required",
                            delivery="unavailable",
                            completeness="unavailable",
                            reason="Source missing",
                        ),
                    ),
                )
            )

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

    def test_brokered_repair_has_one_stdout_contract_without_runtime_helpers(self):
        prompt = self.render(
            "new-pr",
            runtime_context={
                "origin_type": "web",
                "result_transport": "stdout_compact_submission",
                "db_path": "/private/controller.sqlite3",
                "snapshot_script": "/runtime/snapshot.py",
                "heartbeat_script": "/runtime/heartbeat.py",
                "work_item_event_ids": ["reply-1"],
            },
        )
        self.assertIn("Return exactly one compact-v1 new_pr submission JSON object on stdout", prompt)
        self.assertIn("The trusted broker", prompt)
        self.assertIn("Do not access Robert DB or runtime commands", prompt)
        self.assertIn('"consumed_work_item_event_ids": [', prompt)
        self.assertIn('"verification": [', prompt)
        self.assertIn("Follow verification_policy and repository requirements", prompt)
        for forbidden in (
            "/private/controller.sqlite3",
            "/runtime/",
            "Submission acknowledgement:",
            "<<'JSON'",
            "waiting_for_user",
            "recorded or deferred acknowledgement",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, prompt)

    def test_invalid_submission_transport_is_rejected_before_rendering(self):
        with self.assertRaisesRegex(ValueError, "unsupported result transport"):
            self.render("new-pr", runtime_context={"result_transport": "unknown"})
        with self.assertRaisesRegex(ValueError, "restricted repair requires new_pr"):
            self.render("local-result", runtime_context={"result_transport": "stdout_compact_submission"})

    def test_new_pr_discloses_assumptions_and_decisions_in_public_body(self):
        for origin_type in ("github", "web"):
            with self.subTest(origin_type=origin_type):
                prompt = self.render("new-pr", runtime_context={"origin_type": origin_type})
                self.assertIn("PR body: assumptions and decisions", prompt)
                guidance = prompt.split("PR body: assumptions and decisions\n", 1)[1].split("\n\n", 1)[0]
                self.assertIn("GitHub PR body itself", guidance)
                self.assertIn("defaults", guidance)
                self.assertIn("scope exclusions", guidance)
                self.assertIn("conflicting requirements", guidance)
                self.assertIn("user-confirmed requirements", guidance)
                self.assertIn("unverified assumptions", guidance)
                self.assertIn("evidence", guidance)
                self.assertIn("alternatives", guidance)
                self.assertIn("impact", guidance)
                self.assertIn("No material assumptions or discretionary decisions", guidance)
                self.assertIn("does not authorize new scope or resolve missing authorization", guidance)
                self.assertIn("submission contract below", guidance)
                self.assertIn("record --output-type waiting_for_user", prompt)
                payload = json.loads(prompt.split("<<'JSON'\n", 1)[1].split("\nJSON", 1)[0])
                action = next(item for item in payload["planned_github_actions"] if item["type"] == "open_pr")
                self.assertIn("## Assumptions and decisions", action["body"])

    def test_pr_body_guidance_is_absent_without_open_pr_authority(self):
        for route_id, route_config in route.ROUTES.items():
            if route_id == "vote" or "open_pr" in route_config["allowed_github_actions"]:
                continue
            with self.subTest(route_id=route_id):
                prompt = self.render(route_id)
                self.assertNotIn("PR body: assumptions and decisions", prompt)
                self.assertNotIn("## Assumptions and decisions", prompt)

    def test_context_has_one_primary_entry_and_json_is_on_demand(self):
        prompt = self.render("new-pr")
        self.assertIn("Primary archive of the complete request and discussion", prompt)
        self.assertIn("Read /context/input.md.", prompt)
        self.assertIn("Read condition: when structured fields are needed.\n  Read /context/input.json.", prompt)
        self.assertNotIn("Read those files before acting", prompt)

    def test_code_tasks_keep_human_intent_and_verification_limits_visible(self):
        for route_id in ("new-pr", "update-existing-pr"):
            for origin in ("github", "web"):
                with self.subTest(route_id=route_id, origin=origin):
                    prompt = self.render(route_id, runtime_context={"origin_type": origin})
                    guidance = prompt.split("Human collaboration:\n", 1)[1].split("\n\n", 1)[0]
                    for requirement in (
                        "observable acceptance criteria",
                        "do not invent requirements",
                        "existing authorization",
                        "what changed in your understanding",
                        "verification is now stale",
                        "no new evidence",
                        "what it proves",
                        "what it does not prove",
                        "PR body or outcome comment",
                        "review entry points",
                    ):
                        self.assertIn(requirement, guidance)
                    self.assertIn("could disprove the implementation", prompt)
                    self.assertIn("requirements or established contracts", prompt)

    def test_non_code_routes_do_not_receive_implementation_collaboration_workflow(self):
        for route_id, config in route.ROUTES.items():
            if route_id == "vote" or config["expected_output"] in {"new_pr", "update_existing_pr"}:
                continue
            with self.subTest(route_id=route_id):
                self.assertNotIn("Human collaboration:", self.render(route_id))

    def test_vote_has_no_ordinary_worker_prompt(self):
        with self.assertRaisesRegex(ValueError, "no ordinary worker stage"):
            self.render("vote")

    def test_source_review_checks_decisions_and_reports_coverage_limits(self):
        prompt = self.render("review-pr")
        self.assertIn("undisclosed decisions", prompt)
        self.assertIn("unreviewed scope", prompt)
        self.assertIn("not a defect by itself", prompt)

    def test_memory_context_discloses_omitted_details(self):
        memories = [
            {"memory_id": f"m{index}", "short_summary": "Historical hint", "long_summary": f"Detail {index}"}
            for index in range(4)
        ]
        prompt = self.render("new-pr", project_memories=memories)
        self.assertIn("Detail 2", prompt)
        self.assertNotIn("Detail 3", prompt)
        self.assertIn("memory (optional, inline, truncated)", prompt)
        self.assertIn("long summaries after the first three memories are omitted", prompt)
        complete = self.render("new-pr", project_memories=memories[:3])
        self.assertIn("memory (optional, inline, complete)", complete)

    def test_nonempty_memory_keeps_evidence_requirements(self):
        prompt = self.render(
            "new-pr",
            project_memories=[{"memory_id": "m1", "short_summary": "Check callers"}],
        )
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
            worker_prompt_input(
                {
                    "task_id": "task-1",
                    "attempt_id": "attempt-1",
                    "workstream_id": "github:x/y!1",
                },
                route.ROUTES["review-pr"],
                [{"source_type": "pull_request", "pr_author_login": "Robert-Bot"}],
                runtime_context={"github_account": "robert-bot"},
            )
        ).text
        self.assertIn('Configured Robert GitHub account: "robert-bot"', prompt)
        self.assertIn('"decision_mention": "@wklken"', prompt)
        self.assertIn("planned comment body", prompt)
        self.assertIn("Robert adds the confirmed review recipient's mention", prompt)

    def test_review_policy_preserves_recovery_evidence_and_feedback_scope(self):
        prompt = self.render("review-comment")
        self.assertIn("Reuse still-valid edits and verification evidence on recovery", prompt)
        self.assertIn("do not reuse a verdict as proof", prompt)
        self.assertIn("The policy below applies only when evaluating review feedback", prompt)
        self.assertIn(
            "Do not expand supplied-feedback verification into a fresh source review",
            prompt,
        )
        self.assertIn("Do not modify source or plan a push", prompt)

    def test_questions_and_pr_followups_keep_the_original_request_owner(self):
        owner = {"login": "Alice", "source_key": "github:x/y#1", "event_fingerprint": "comment:1"}
        for route_id in ("new-pr", "waiting-for-user", "update-existing-pr"):
            with self.subTest(route_id=route_id):
                prompt = self.render(route_id, runtime_context={"decision_owner": owner})
                self.assertIn('"login": "Alice"', prompt)
                self.assertIn("confirmed decision owner", prompt)

    def test_single_review_prompt_addresses_author_and_preserves_robot_request_owner(self):
        for author, expected in (("RimuruChan", "RimuruChan"), ("Robert-Bot", "Alice")):
            with self.subTest(author=author):
                prompt = render_prompt.render_prompt(
                    worker_prompt_input(
                        {"task_id": "t", "attempt_id": "a", "workstream_id": "github:x/y!1"},
                        route.ROUTES["review-pr"],
                        [{"source_type": "pull_request", "pr_author_login": author, "requester_login": "Bob"}],
                        runtime_context={"github_account": "robert-bot", "decision_owner": {"login": "Alice"}},
                    )
                ).text
                self.assertIn(f'"decision_mention": "@{expected}"', prompt)

    def test_issue_dispatch_guidance_follows_current_request_not_older_context(self):
        task = {"task_id": "task-1", "attempt_id": "attempt-1", "workstream_id": "github:x/y#1"}
        dispatch = {"event_fingerprint": "operator-dispatch:one", "dispatch_request_id": "one"}
        followup = {"event_fingerprint": "comment:2", "event_type": "comment"}
        for current, expected in (("operator-dispatch:one", True), ("comment:2", False)):
            with self.subTest(current=current):
                request = worker_prompt_input(
                    task,
                    route.ROUTES["classification-result"],
                    [dispatch, followup],
                    runtime_context={"github_context": {"current_event_fingerprint": current}},
                )
                prompt = render_prompt.render_prompt(request).text
                self.assertEqual("Issue dispatch:" in prompt, expected)
                if expected:
                    self.assertIn("Never omit recommended_route", prompt)
                    self.assertIn("result comment on the original Issue", prompt)


if __name__ == "__main__":
    unittest.main()
