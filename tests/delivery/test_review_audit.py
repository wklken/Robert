from tests.support.results import internal_result
from tests.support.worker_protocol import WorkerProtocolFixture


class ReviewAuditTests(WorkerProtocolFixture):
    def test_update_existing_pr_result_accepts_push_existing_pr_action_with_response_comment(
        self,
    ):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="update_existing_pr",
            planned_github_actions=[
                {
                    "type": "push_existing_pr",
                    "worktree_path": "/repo/.worktrees/dd-123",
                    "branch": "codex/dd-123",
                    "target_url": "https://github.com/x/y/pull/1",
                },
                {
                    "type": "comment",
                    "target_url": "https://github.com/x/y/pull/1#pullrequestreview-9",
                    "body": self._dd_comment_body(
                        "Implemented the valid review point and pushed the branch."
                    ),
                },
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="updated existing pr",
            used_skills=["fast-verify-review-point"],
            review_point_evaluation=[self._review_point("correct", "implement")],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "comment"],
        )

        self.assertEqual(audit["status"], "accepted")

    def test_update_existing_pr_push_without_response_comment_fails_audit(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="update_existing_pr",
            planned_github_actions=[
                {
                    "type": "push_existing_pr",
                    "worktree_path": "/repo/.worktrees/dd-123",
                    "branch": "codex/dd-123",
                }
            ],
            consumed_event_fingerprints=["review:1"],
            verification=[],
            handoff="updated existing pr",
            used_skills=["fast-verify-review-point"],
            review_point_evaluation=[self._review_point("correct", "implement")],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "comment"],
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("comment", audit["safe_error"])

    def test_update_existing_pr_response_comment_must_follow_push_action(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="update_existing_pr",
            planned_github_actions=[
                self._review_response_action(
                    "Responding before the push is not allowed."
                ),
                {
                    "type": "push_existing_pr",
                    "worktree_path": "/repo/.worktrees/dd-123",
                    "branch": "codex/dd-123",
                },
            ],
            consumed_event_fingerprints=["review:1"],
            verification=[],
            handoff="updated existing pr",
            used_skills=["fast-verify-review-point"],
            review_point_evaluation=[self._review_point("correct", "implement")],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "comment"],
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("after push_existing_pr", audit["safe_error"])

    def test_push_existing_pr_without_review_point_evaluation_creates_policy_violation(
        self,
    ):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="update_existing_pr",
            planned_github_actions=[
                {
                    "type": "push_existing_pr",
                    "worktree_path": "/repo/.worktrees/dd-123",
                    "branch": "codex/dd-123",
                }
            ]
            + [self._review_response_action()],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="updated existing pr",
            used_skills=["fast-verify-review-point"],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "comment"],
        )

        self.assertEqual(audit["status"], "policy_violation")
        self.assertEqual(audit["violations"], ["missing_review_point_evaluation"])

    def test_push_existing_pr_with_all_rejected_review_points_creates_policy_violation(
        self,
    ):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="update_existing_pr",
            planned_github_actions=[
                {
                    "type": "push_existing_pr",
                    "worktree_path": "/repo/.worktrees/dd-123",
                    "branch": "codex/dd-123",
                }
            ]
            + [self._review_response_action("The review point is not valid.")],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="review point rejected",
            used_skills=["fast-verify-review-point"],
            review_point_evaluation=[self._review_point("incorrect", "comment")],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "comment"],
        )

        self.assertEqual(audit["status"], "policy_violation")
        self.assertEqual(audit["violations"], ["no_implemented_review_points"])

    def test_push_existing_pr_with_invalid_review_point_entry_creates_policy_violation(
        self,
    ):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="update_existing_pr",
            planned_github_actions=[
                {
                    "type": "push_existing_pr",
                    "worktree_path": "/repo/.worktrees/dd-123",
                    "branch": "codex/dd-123",
                }
            ]
            + [self._review_response_action()],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="bad review point record",
            used_skills=["fast-verify-review-point"],
            review_point_evaluation=[
                {
                    "summary": "Fix the branch push",
                    "verdict": "correct",
                    "action": "implement",
                }
            ],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "comment"],
        )

        self.assertEqual(audit["status"], "policy_violation")
        self.assertEqual(audit["violations"], ["invalid_review_point_evaluation"])

    def test_push_existing_pr_cannot_implement_incorrect_review_point(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="update_existing_pr",
            planned_github_actions=[
                {
                    "type": "push_existing_pr",
                    "worktree_path": "/repo/.worktrees/dd-123",
                    "branch": "codex/dd-123",
                }
            ]
            + [self._review_response_action()],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="bad review point record",
            used_skills=["fast-verify-review-point"],
            review_point_evaluation=[self._review_point("incorrect", "implement")],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "comment"],
        )

        self.assertEqual(audit["status"], "policy_violation")
        self.assertEqual(audit["violations"], ["invalid_review_point_evaluation"])

    def test_update_existing_pr_comment_only_requires_review_point_evaluation(self):
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
        )

        self.assertEqual(audit["status"], "accepted")

    def test_update_existing_pr_mergeable_report_accepts_comment_only_response(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="update_existing_pr",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/x/y/pull/1#pullrequestreview-9",
                    "body": self._dd_comment_body(
                        "The review report has no blocking items; no code change is needed."
                    ),
                }
            ],
            consumed_event_fingerprints=["review:9"],
            verification=[],
            handoff="commented on mergeable review report",
            used_skills=["fast-verify-review-point"],
            review_point_evaluation=[
                {
                    "summary": "Review report has no blocking items and says the PR can merge.",
                    "verdict": "correct",
                    "reasoning": "The report requires no code changes, only an acknowledgement.",
                    "action": "comment",
                }
            ],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "comment"],
        )

        self.assertEqual(audit["status"], "accepted")

    def test_update_existing_pr_comment_only_without_review_point_evaluation_fails(
        self,
    ):
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
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "comment"],
        )

        self.assertEqual(audit["status"], "policy_violation")
        self.assertEqual(audit["violations"], ["missing_review_point_evaluation"])

    def test_review_comment_result_requires_review_point_evaluation(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="review_comment",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/x/y/pull/1",
                    "body": self._dd_comment_body("Reviewed the PR."),
                }
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="reviewed PR",
            used_skills=["fast-verify-review-point"],
            review_point_evaluation=[self._review_point("correct", "comment")],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["comment"],
        )

        self.assertEqual(audit["status"], "accepted")

    def test_pr_review_comment_result_does_not_require_review_point_evaluation(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="pr_review_comment",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/x/y/pull/1",
                    "pr_author_login": "pr-author",
                    "body": self._dd_comment_body("@pr-author Reviewed the PR source."),
                }
            ],
            consumed_event_fingerprints=["review_request:1"],
            verification=[],
            handoff="reviewed PR source",
            used_skills=["fast-review-github-pr"],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["comment"],
            required_skills=["fast-review-github-pr"],
            expected_output="pr_review_comment",
        )

        self.assertEqual(audit["status"], "accepted")

    def test_pr_review_comment_requires_pr_author_login(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="pr_review_comment",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/x/y/pull/1",
                    "body": self._dd_comment_body("@pr-author Reviewed the PR source."),
                }
            ],
            consumed_event_fingerprints=["review_request:1"],
            verification=[],
            handoff="reviewed PR source",
            used_skills=["fast-review-github-pr"],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["comment"],
            required_skills=["fast-review-github-pr"],
            expected_output="pr_review_comment",
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("pr_author_login", audit["safe_error"])

    def test_pr_review_comment_requires_mentioning_pr_author(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="pr_review_comment",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/x/y/pull/1",
                    "pr_author_login": "pr-author",
                    "body": self._dd_comment_body("Reviewed the PR source."),
                }
            ],
            consumed_event_fingerprints=["review_request:1"],
            verification=[],
            handoff="reviewed PR source",
            used_skills=["fast-review-github-pr"],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["comment"],
            required_skills=["fast-review-github-pr"],
            expected_output="pr_review_comment",
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("@pr-author", audit["safe_error"])

    def test_pr_review_comment_requires_fast_review_skill(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="pr_review_comment",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/x/y/pull/1",
                    "pr_author_login": "pr-author",
                    "body": self._dd_comment_body("@pr-author Reviewed the PR source."),
                }
            ],
            consumed_event_fingerprints=["review_request:1"],
            verification=[],
            handoff="reviewed PR source",
            used_skills=[],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["comment"],
            required_skills=["fast-review-github-pr"],
            expected_output="pr_review_comment",
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("fast-review-github-pr", audit["safe_error"])

    def test_required_skill_missing_from_used_skills_fails_audit(self):
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
            used_skills=[],
            review_point_evaluation=[self._review_point("incorrect", "comment")],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "comment"],
            required_skills=["fast-verify-review-point"],
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("fast-verify-review-point", audit["safe_error"])

    def test_update_existing_pr_result_without_type_fails_audit(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="update_existing_pr",
            planned_github_actions=[
                {"action": "push_existing_pr", "url": "https://github.com/x/y/pull/1"}
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="updated existing pr",
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "comment"],
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("planned_github_actions[*].type", audit["safe_error"])
