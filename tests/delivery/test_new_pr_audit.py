from tests.support.results import internal_result
from tests.support.worker_protocol import WorkerProtocolFixture


class NewPRAuditTests(WorkerProtocolFixture):
    def test_result_with_extra_action_creates_policy_violation(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[{"type": "open_pr", "url": "https://github.com/x/y/pull/2"}],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="opened pr",
        )

        audit = audit_result.audit_result(payload, allowed_github_actions=["comment"])

        self.assertEqual(audit["status"], "policy_violation")
        self.assertEqual(audit["violations"], ["open_pr"])

    def test_new_pr_result_requires_open_pr_action(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="new_pr",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="claimed new PR",
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["open_pr", "comment"],
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("output_type", audit["safe_error"])

    def test_new_pr_result_accepts_push_then_open_pr_actions(self):
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
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "open_pr", "comment"],
        )

        self.assertEqual(audit["status"], "accepted")

    def test_issue_sourced_new_pr_rejects_complete_resolution_with_refs(self):
        from robert_agent.delivery.application import audit as audit_result

        audit = audit_result.audit_result(
            self._issue_new_pr_result(
                issue_resolution="complete",
                issue_reference="Refs #1",
            ),
            allowed_github_actions=["push_existing_pr", "open_pr"],
            action_scope=self._issue_action_scope(),
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("closing keyword", audit["safe_error"])

    def test_issue_sourced_partial_pr_requires_remaining_scope(self):
        from robert_agent.delivery.application import audit as audit_result

        audit = audit_result.audit_result(
            self._issue_new_pr_result(
                issue_resolution="partial",
                issue_reference="Refs #1",
            ),
            allowed_github_actions=["push_existing_pr", "open_pr"],
            action_scope=self._issue_action_scope(),
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("remaining_scope", audit["safe_error"])

    def test_issue_sourced_partial_pr_accepts_refs_with_remaining_scope(self):
        from robert_agent.delivery.application import audit as audit_result

        audit = audit_result.audit_result(
            self._issue_new_pr_result(
                issue_resolution="partial",
                issue_reference="Refs #1",
                remaining_scope="Standard-input credential import remains a follow-up.",
            ),
            allowed_github_actions=["push_existing_pr", "open_pr"],
            action_scope=self._issue_action_scope(),
        )

        self.assertEqual(audit["status"], "accepted")

    def test_issue_sourced_partial_pr_rejects_closing_keyword(self):
        from robert_agent.delivery.application import audit as audit_result

        audit = audit_result.audit_result(
            self._issue_new_pr_result(
                issue_resolution="partial",
                issue_reference="Refs #1\nFixes #1",
                remaining_scope="Standard-input credential import remains a follow-up.",
            ),
            allowed_github_actions=["push_existing_pr", "open_pr"],
            action_scope=self._issue_action_scope(),
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("must not use a closing keyword", audit["safe_error"])

    def test_issue_sourced_complete_pr_accepts_closing_reference(self):
        from robert_agent.delivery.application import audit as audit_result

        audit = audit_result.audit_result(
            self._issue_new_pr_result(
                issue_resolution="complete",
                issue_reference="Fixes #1",
            ),
            allowed_github_actions=["push_existing_pr", "open_pr"],
            action_scope=self._issue_action_scope(),
        )

        self.assertEqual(audit["status"], "accepted")

    def test_issue_sourced_closing_reference_requires_explicit_resolution(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = self._issue_new_pr_result(
            issue_resolution="complete",
            issue_reference="Fixes #1",
        )
        payload["planned_github_actions"][1].pop("issue_resolution")

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "open_pr"],
            action_scope=self._issue_action_scope(),
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("issue_resolution", audit["safe_error"])
