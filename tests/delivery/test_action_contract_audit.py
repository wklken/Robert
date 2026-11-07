from tests.support.results import internal_result
from tests.support.worker_protocol import WorkerProtocolFixture


class ActionContractAuditTests(WorkerProtocolFixture):
    def test_comment_action_without_idempotency_marker_fails_audit(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/x/y/issues/1",
                    "body": "Analysis is ready",
                }
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="commented analysis",
        )

        audit = audit_result.audit_result(payload, allowed_github_actions=["comment"])

        self.assertEqual(audit["status"], "failed")
        self.assertIn("robert-comment", audit["safe_error"])

    def test_open_pr_action_missing_publisher_fields_fails_audit(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="new_pr",
            planned_github_actions=[
                {
                    "type": "open_pr",
                    "repo": "x/y",
                    "head": "codex/dd-123",
                    "body": self._dd_pr_body("Implements the fix"),
                }
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="opened pr",
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["open_pr", "comment"],
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("open_pr action missing fields", audit["safe_error"])
        self.assertIn("base", audit["safe_error"])
        self.assertIn("title", audit["safe_error"])

    def test_open_pr_action_without_workstream_marker_fails_audit(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="new_pr",
            planned_github_actions=[
                {
                    "type": "open_pr",
                    "repo": "x/y",
                    "head": "codex/dd-123",
                    "base": "master",
                    "title": "Fix timeout",
                    "body": "Implements the fix",
                }
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="opened pr",
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["open_pr", "comment"],
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("robert-workstream", audit["safe_error"])

    def test_push_existing_pr_action_missing_publisher_fields_fails_audit(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="update_existing_pr",
            planned_github_actions=[
                {"type": "push_existing_pr", "target_url": "https://github.com/x/y/pull/1"}
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
        self.assertIn("push_existing_pr action missing fields", audit["safe_error"])
        self.assertIn("worktree_path", audit["safe_error"])
        self.assertIn("branch", audit["safe_error"])

    def test_result_with_sensitive_public_action_text_creates_policy_violation(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[
                {
                    "type": "comment",
                    "url": "https://github.com/x/y/issues/1#comment",
                    "body": "Authorization" + ": Bearer secret",
                }
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="commented analysis",
        )

        audit = audit_result.audit_result(payload, allowed_github_actions=["comment"])

        self.assertEqual(audit["status"], "policy_violation")
        self.assertEqual(audit["violations"], ["redaction_blocked"])

    def test_result_with_local_public_action_text_requires_redaction(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[
                {
                    "type": "comment",
                    "url": "https://github.com/x/y/issues/1#comment",
                    "body": (
                        "failed at /Users/sample-user/project "
                        "with 10.0.0.2"
                    ),
                }
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="commented analysis",
        )

        audit = audit_result.audit_result(payload, allowed_github_actions=["comment"])

        self.assertEqual(audit["status"], "policy_violation")
        self.assertEqual(audit["violations"], ["redaction_required"])

    def test_result_missing_consumed_events_fails_audit(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[],
            consumed_event_fingerprints=[],
            verification=[],
            handoff="no events",
        )

        audit = audit_result.audit_result(payload, allowed_github_actions=[])

        self.assertEqual(audit["status"], "failed")
        self.assertIn("consumed_event_fingerprints", audit["safe_error"])
