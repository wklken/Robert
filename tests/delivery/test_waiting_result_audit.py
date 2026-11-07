from tests.support.results import internal_result
from tests.support.worker_protocol import WorkerProtocolFixture


class WaitingResultAuditTests(WorkerProtocolFixture):
    def test_blocked_execution_cannot_complete_with_waiting_route_recommendation(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="update_existing_pr",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/x/y/pull/1",
                    "body": self._dd_comment_body("Blocked on task scope; no changes applied."),
                }
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="BLOCKED: task scope must be clarified before editing.",
            used_skills=[],
            recommended_route="waiting-for-user",
            review_point_evaluation=[
                {
                    "summary": "Repository document needs updating",
                    "verdict": "correct",
                    "reasoning": "The task scope conflicts with the authorized document edit.",
                    "action": "clarify",
                }
            ],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "comment"],
            expected_output="update_existing_pr",
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("output_type waiting_for_user", audit["safe_error"])

    def test_classification_can_recommend_waiting_route(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="classification_result",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="A scope decision is required before implementation.",
            used_skills=[],
            recommended_route="waiting-for-user",
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=[],
            expected_output="classification_result",
        )

        self.assertEqual(audit["status"], "accepted")

    def test_blocked_execution_accepts_waiting_output_and_scope_question(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="waiting_for_user",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/x/y/pull/1",
                    "body": self._dd_comment_body("Please clarify the task scope."),
                }
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="Waiting for task scope clarification.",
            used_skills=[],
            recommended_route="waiting-for-user",
            operator_question={
                "kind": "scope_decision",
                "summary": "May this task modify the specified repository document?",
            },
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "comment"],
            expected_output="update_existing_pr",
        )

        self.assertEqual(audit["status"], "accepted")

    def test_web_waiting_result_accepts_local_event_and_structured_question(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-local",
            attempt_id="attempt-local",
            output_type="waiting_for_user",
            planned_github_actions=[],
            consumed_event_fingerprints=[],
            consumed_work_item_event_ids=["wie-1"],
            operator_question={
                "kind": "clarification",
                "summary": "Which compatibility target should be kept?",
                "choices": [
                    {"id": "keep-v1", "label": "Keep v1 compatibility"},
                    {"id": "v2-only", "label": "Allow a v2-only change"},
                ],
            },
            verification=[],
            handoff="waiting for a compatibility decision",
            used_skills=[],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=[],
            expected_output="local_result",
            origin_type="web",
        )

        self.assertEqual(audit["status"], "accepted")

    def test_waiting_result_without_question_fails(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-local",
            attempt_id="attempt-local",
            output_type="waiting_for_user",
            planned_github_actions=[],
            consumed_event_fingerprints=[],
            consumed_work_item_event_ids=["wie-1"],
            verification=[],
            handoff="waiting",
            used_skills=[],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=[],
            expected_output="local_result",
            origin_type="web",
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("operator_question", audit["safe_error"])

    def test_web_result_requires_local_event_and_rejects_github_action(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-local",
            attempt_id="attempt-local",
            output_type="local_result",
            planned_github_actions=[],
            consumed_event_fingerprints=[],
            consumed_work_item_event_ids=[],
            verification=[],
            handoff="done",
            used_skills=[],
        )
        missing = audit_result.audit_result(
            payload,
            allowed_github_actions=[],
            expected_output="local_result",
            origin_type="web",
        )
        payload["consumed_work_item_event_ids"] = ["wie-1"]
        payload["planned_github_actions"] = [
            {
                "type": "comment",
                "target_url": "https://github.com/example/repo/issues/1",
                "body": self._dd_comment_body("Public reply"),
            }
        ]
        public = audit_result.audit_result(
            payload,
            allowed_github_actions=["comment"],
            expected_output="local_result",
            origin_type="web",
        )

        self.assertIn("consumed_work_item_event_ids", missing["safe_error"])
        self.assertEqual(public["status"], "policy_violation")
