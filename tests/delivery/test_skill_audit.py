from tests.support.results import internal_result
from tests.support.worker_protocol import WorkerProtocolFixture


class SkillAuditTests(WorkerProtocolFixture):
    def test_result_audit_accepts_missing_recommended_skills(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/x/y/issues/1",
                    "body": self._dd_comment_body("Commented analysis"),
                }
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="commented analysis",
            used_skills=[],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["comment"],
            recommended_skills=["fast-code-path"],
        )

        self.assertEqual(audit["status"], "accepted")

    def test_result_audit_accepts_extra_installed_skills(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/x/y/issues/1",
                    "body": self._dd_comment_body("Commented analysis"),
                }
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="commented analysis",
            used_skills=["fast-code-path", "fast-small-pr"],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["comment"],
            required_skills=["fast-code-path"],
        )

        self.assertEqual(audit["status"], "accepted")
