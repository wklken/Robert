from tests.support.results import internal_result
from tests.support.worker_protocol import WorkerProtocolFixture


class VerificationAuditTests(WorkerProtocolFixture):
    def test_new_pr_route_policy_rejects_empty_verification(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="new_pr",
            planned_github_actions=[
                {
                    "type": "push_existing_pr",
                    "worktree_path": "/repo/.worktrees/dd-123",
                    "branch": "codex/dd-123",
                },
                {
                    "type": "open_pr",
                    "repo": "x/y",
                    "head": "codex/dd-123",
                    "base": "master",
                    "title": "Fix timeout",
                    "body": self._dd_pr_body("Implements the fix"),
                },
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="opened pr",
            used_skills=[],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "open_pr", "comment"],
            verification_policy={
                "mode": "required",
                "required_statuses": ["passed"],
                "allow_skipped": False,
            },
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("verification policy failed", audit["safe_error"])

    def test_new_pr_route_policy_accepts_required_passed_verification(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="new_pr",
            planned_github_actions=[
                {
                    "type": "push_existing_pr",
                    "worktree_path": "/repo/.worktrees/dd-123",
                    "branch": "codex/dd-123",
                },
                {
                    "type": "open_pr",
                    "repo": "x/y",
                    "head": "codex/dd-123",
                    "base": "master",
                    "title": "Fix timeout",
                    "body": self._dd_pr_body("Implements the fix"),
                },
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[self._verification_entry()],
            handoff="opened pr",
            used_skills=[],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "open_pr", "comment"],
            verification_policy={
                "mode": "required",
                "required_statuses": ["passed"],
                "allow_skipped": False,
            },
        )

        self.assertEqual(audit["status"], "accepted")

    def test_independent_failed_required_check_blocks_publication(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="new_pr",
            planned_github_actions=[
                {
                    "type": "push_existing_pr",
                    "worktree_path": "/repo/.worktrees/dd-123",
                    "branch": "codex/dd-123",
                },
                {
                    "type": "open_pr",
                    "repo": "x/y",
                    "head": "codex/dd-123",
                    "base": "master",
                    "title": "Fix timeout",
                    "body": self._dd_pr_body("Implements the fix"),
                },
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[
                self._verification_entry(),
                {**self._verification_entry(status="failed"), "command": "python -m unittest", "exit_code": 1},
            ],
            handoff="opened pr",
            used_skills=[],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "open_pr", "comment"],
            verification_policy={
                "mode": "required",
                "required_statuses": ["passed"],
                "allow_skipped": False,
            },
        )

        self.assertEqual(audit["status"], "failed")

    def test_route_policy_rejects_skipped_required_verification(self):
        from robert_agent.delivery.application import audit as audit_result

        skipped = self._verification_entry(status="skipped")
        skipped["skipped_reason"] = "not run"
        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="new_pr",
            planned_github_actions=[
                {
                    "type": "push_existing_pr",
                    "worktree_path": "/repo/.worktrees/dd-123",
                    "branch": "codex/dd-123",
                },
                {
                    "type": "open_pr",
                    "repo": "x/y",
                    "head": "codex/dd-123",
                    "base": "master",
                    "title": "Fix timeout",
                    "body": self._dd_pr_body("Implements the fix"),
                },
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[skipped],
            handoff="opened pr",
            used_skills=[],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "open_pr", "comment"],
            verification_policy={
                "mode": "required",
                "required_statuses": ["passed"],
                "allow_skipped": False,
            },
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("required verification cannot be skipped", audit["safe_error"])

    def test_update_existing_pr_comment_only_policy_allows_empty_verification(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="update_existing_pr",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/x/y/pull/1#discussion_r1",
                    "body": self._dd_comment_body("The review point is not valid."),
                }
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="commented on rejected review point",
            used_skills=["fast-verify-review-point"],
            review_point_evaluation=[self._review_point("incorrect", "comment")],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "comment"],
            verification_policy={
                "mode": "required_for_push",
                "required_statuses": ["passed"],
                "allow_skipped": False,
            },
        )

        self.assertEqual(audit["status"], "accepted")

    def test_result_output_type_must_match_expected_output(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/x/y/issues/1",
                    "body": self._dd_comment_body("Analysis only"),
                }
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="analysis only",
            used_skills=[],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["open_pr", "comment"],
            expected_output="new_pr",
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("expected_output new_pr", audit["safe_error"])

    def test_final_audit_rejects_inconsistent_execution_evidence(self):
        from robert_agent.delivery.application.audit import _audit_verification_entry

        for entry in (
            {"status": "passed"},
            {"status": "passed", "exit_code": 1},
            {"status": "failed", "exit_code": 0},
            {"status": "passed", "exit_code": False},
            {"status": "skipped", "exit_code": 0, "skipped_reason": "not run"},
        ):
            with self.subTest(entry=entry):
                self.assertIsNotNone(
                    _audit_verification_entry(
                        {"command": "check", "purpose": "verify behavior", "required": True, **entry}, True
                    )
                )
