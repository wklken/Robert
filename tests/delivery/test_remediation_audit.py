from tests.support.results import internal_result
from tests.support.worker_protocol import WorkerProtocolFixture


class RemediationAuditTests(WorkerProtocolFixture):
    def test_remediation_audit_requires_matching_trusted_evidence_and_attestation(self):
        from robert_agent.delivery.application import audit as audit_result

        evidence = {
            "kind": "ci",
            "episode_id": "episode-ci",
            "observed_head_sha": "head-1",
            "observed_base_sha": "base-1",
            "failure_signature": "failure-1",
            "resolution_summary": "Fixed the failing unit test.",
        }
        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="update_existing_pr",
            planned_github_actions=[
                {
                    "type": "push_existing_pr",
                    "worktree_path": "/tmp/worktree",
                    "branch": "codex/fix",
                },
                {
                    "type": "comment",
                    "target_url": "https://github.com/x/y/pull/1",
                    "body": self._dd_comment_body("Fixed CI"),
                },
            ],
            consumed_event_fingerprints=["github-system:workflow_run:101:1"],
            verification=[
                {
                    "command": "python -m unittest",
                    "status": "passed",
                    "purpose": "Verify the CI repair.",
                    "required": True,
                    "exit_code": 0,
                }
            ],
            handoff="fixed",
            used_skills=["fast-test-fix", "fast-preflight"],
            remediation_evidence=evidence,
        )
        context = {
            "episode_id": "episode-ci",
            "episode_kind": "ci",
            "observed_head_sha": "head-1",
            "observed_base_sha": "base-1",
            "failure_signature": "failure-1",
        }
        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "comment"],
            required_skills=["fast-test-fix", "fast-preflight"],
            expected_output="update_existing_pr",
            verification_policy={
                "mode": "required_for_push",
                "required_statuses": ["passed"],
                "allow_skipped": False,
            },
            task_kind="ci_remediation",
            remediation_context=context,
            remediation_attestation={
                "status": "accepted",
                "result_head_sha": "repair-1",
            },
        )

        self.assertEqual(audit["status"], "accepted")
        self.assertNotIn("missing_review_point_evaluation", audit.get("violations", []))

        mismatched = audit_result.audit_result(
            {**payload, "remediation_evidence": {**evidence, "episode_id": "forged"}},
            allowed_github_actions=["push_existing_pr", "comment"],
            required_skills=["fast-test-fix", "fast-preflight"],
            expected_output="update_existing_pr",
            verification_policy={
                "mode": "required_for_push",
                "required_statuses": ["passed"],
                "allow_skipped": False,
            },
            task_kind="ci_remediation",
            remediation_context=context,
            remediation_attestation={
                "status": "accepted",
                "result_head_sha": "repair-1",
            },
        )
        self.assertEqual(mismatched["status"], "policy_violation")
        self.assertEqual(
            mismatched["violations"],
            ["remediation_evidence_mismatch"],
        )
