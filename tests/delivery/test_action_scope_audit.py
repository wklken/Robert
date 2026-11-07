from tests.support.results import internal_result
from tests.support.worker_protocol import WorkerProtocolFixture


class ActionScopeAuditTests(WorkerProtocolFixture):
    def test_result_with_allowed_actions_audits_clean(self):
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
            used_skills=["fast-code-path"],
        )

        action_scope = {
            "repo": "x/y",
            "sources": [{"source_type": "issue", "number": 1}],
        }
        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["comment"],
            action_scope=action_scope,
        )

        self.assertEqual(audit["status"], "accepted")
        self.assertEqual(audit["action_scope"], action_scope)

    def test_result_comment_target_must_match_action_scope_source(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/x/y/issues/2",
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
            action_scope={
                "repo": "x/y",
                "sources": [{"source_type": "issue", "number": 1}],
            },
        )

        self.assertEqual(audit["status"], "policy_violation")
        self.assertEqual(audit["violations"], ["action_scope"])

    def test_result_open_pr_target_must_match_action_scope(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="new_pr",
            planned_github_actions=[
                {
                    "type": "push_existing_pr",
                    "worktree_path": "/tmp/worktree",
                    "branch": "codex/dd-123",
                },
                {
                    "type": "open_pr",
                    "repo": "other/repo",
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
            allowed_github_actions=["push_existing_pr", "open_pr"],
            action_scope={
                "repo": "x/y",
                "base_branch": "master",
                "worktree_path": "/tmp/worktree",
                "branch_name": "codex/dd-123",
            },
        )

        self.assertEqual(audit["status"], "policy_violation")
        self.assertEqual(audit["violations"], ["action_scope"])

    def test_result_push_target_must_match_action_scope(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="new_pr",
            planned_github_actions=[
                {
                    "type": "push_existing_pr",
                    "worktree_path": "/tmp/other-worktree",
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
            allowed_github_actions=["push_existing_pr", "open_pr"],
            action_scope={
                "repo": "x/y",
                "base_branch": "master",
                "worktree_path": "/tmp/worktree",
                "branch_name": "codex/dd-123",
            },
        )

        self.assertEqual(audit["status"], "policy_violation")
        self.assertEqual(audit["violations"], ["action_scope"])
