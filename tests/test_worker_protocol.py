from contextlib import closing
import json
import sys
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class WorkerProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_snapshot_builds_phase_row(self):
        from robert_agent.worker import snapshot

        row = snapshot.build_snapshot(
            task_id="task-1",
            attempt_id="attempt-1",
            phase="analyze",
            status="running",
            summary="Reading code",
            next_step="Plan fix",
        )

        self.assertEqual(row["phase"], "analyze")
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["task_id"], "task-1")

    def test_heartbeat_command_record_tracks_active_command(self):
        from robert_agent.worker import heartbeat

        record = heartbeat.build_command_record(
            task_id="task-1",
            attempt_id="attempt-1",
            phase="verify",
            command=["python", "-m", "unittest"],
        )

        self.assertEqual(record["phase"], "verify")
        self.assertEqual(record["active_command"], ["python", "-m", "unittest"])

    def test_result_with_allowed_actions_audits_clean(self):
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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

        audit = audit_result.audit_result(payload, allowed_github_actions=["comment"])

        self.assertEqual(audit["status"], "accepted")

    def test_result_audit_accepts_missing_recommended_skills(self):
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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

    def test_result_recording_allows_no_external_skills(self):
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="analysis complete",
            used_skills=[],
        )

        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)

    def test_result_audit_accepts_extra_installed_skills(self):
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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

    def test_update_existing_pr_result_accepts_push_existing_pr_action_with_response_comment(self):
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
                    "body": self._dd_comment_body("Implemented the valid review point and pushed the branch."),
                }
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="update_existing_pr",
            planned_github_actions=[
                self._review_response_action("Responding before the push is not allowed."),
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

    def test_push_existing_pr_without_review_point_evaluation_creates_policy_violation(self):
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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

    def test_push_existing_pr_with_all_rejected_review_points_creates_policy_violation(self):
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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

    def test_push_existing_pr_with_invalid_review_point_entry_creates_policy_violation(self):
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
                {"summary": "Fix the branch push", "verdict": "correct", "action": "implement"}
            ],
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=["push_existing_pr", "comment"],
        )

        self.assertEqual(audit["status"], "policy_violation")
        self.assertEqual(audit["violations"], ["invalid_review_point_evaluation"])

    def test_push_existing_pr_cannot_implement_incorrect_review_point(self):
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="update_existing_pr",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/x/y/pull/1#pullrequestreview-9",
                    "body": self._dd_comment_body("The review report has no blocking items; no code change is needed."),
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

    def test_update_existing_pr_comment_only_without_review_point_evaluation_fails(self):
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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

    def test_result_with_extra_action_creates_policy_violation(self):
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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

    def test_new_pr_route_policy_rejects_empty_verification(self):
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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

    def test_route_policy_rejects_skipped_required_verification(self):
        from robert_agent import audit_result
        from robert_agent.worker import result

        skipped = self._verification_entry(status="skipped")
        skipped["skipped_reason"] = "not run"
        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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

    def test_classification_result_records_recommended_route(self):
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="classification_result",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="Recommend route new-pr",
            used_skills=[],
            recommended_route="new-pr",
            branch_slug="model-service-connectivity-test",
        )

        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn:
            metadata = json.loads(
                conn.execute(
                    "SELECT metadata_json FROM worker_results WHERE result_id = ?",
                    (record["result_id"],),
                ).fetchone()[0]
            )
        self.assertEqual(payload["recommended_route"], "new-pr")
        self.assertEqual(metadata["recommended_route"], "new-pr")
        self.assertEqual(payload["branch_slug"], "model-service-connectivity-test")
        self.assertEqual(metadata["branch_slug"], "model-service-connectivity-test")

    def test_classification_result_rejects_invalid_branch_slug(self):
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="classification_result",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="Recommend route new-pr",
            used_skills=[],
            recommended_route="new-pr",
            branch_slug="Model Service Connectivity Test",
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=[],
            expected_output="classification_result",
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("branch_slug", audit["safe_error"])

    def test_comment_action_without_idempotency_marker_fails_audit(self):
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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

    def test_web_waiting_result_accepts_local_event_and_structured_question(self):
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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

    def test_snapshot_and_result_write_sqlite_rows(self):
        from robert_agent.worker import result
        from robert_agent.worker import snapshot

        db_path = self._init_attempt_db()

        snapshot_result = snapshot.record_snapshot(
            db_path=db_path,
            task_id="task-1",
            attempt_id="attempt-1",
            phase="verify",
            status="running",
            summary="Running focused tests",
            next_step="Record result",
        )
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[{"type": "comment", "url": "https://github.com/x/y/issues/1#comment"}],
            consumed_event_fingerprints=["comment:1"],
            verification=[{"command": "python -m unittest", "status": "passed"}],
            handoff="done",
            used_skills=["fast-code-path"],
        )
        result_result = result.record_result(db_path, payload)

        self.assertTrue(snapshot_result["ok"], snapshot_result)
        self.assertTrue(result_result["ok"], result_result)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            phase_count = conn.execute("SELECT COUNT(*) FROM worker_phases").fetchone()[0]
            result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
            action_count = conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0]
        self.assertEqual(phase_count, 1)
        self.assertEqual(result_count, 1)
        self.assertEqual(action_count, 1)

    def test_result_records_worker_result_ready_wakeup(self):
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[{"type": "comment", "url": "https://github.com/x/y/issues/1#comment"}],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="done",
            used_skills=["fast-code-path"],
        )

        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            rows = conn.execute(
                """
                SELECT repo_id, reason, dedupe_key, task_id, attempt_id,
                       result_id, status, metadata_json
                FROM wakeups
                """
            ).fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row[:7], ("repo-1", "worker_result_ready", record["result_id"], "task-1", "attempt-1", record["result_id"], "pending"))
        metadata = json.loads(row[7])
        self.assertEqual(metadata["recorded_by"], "robert.worker")
        self.assertEqual(metadata["output_type"], "comment_analysis")

    def test_duplicate_result_does_not_create_extra_wakeup(self):
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[{"type": "comment", "url": "https://github.com/x/y/issues/1#comment"}],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="done",
            used_skills=["fast-code-path"],
        )

        first = result.record_result(db_path, payload)
        with self.assertRaises(sqlite3.IntegrityError):
            result.record_result(db_path, payload)

        self.assertTrue(first["ok"], first)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            wakeup_count = conn.execute("SELECT COUNT(*) FROM wakeups").fetchone()[0]
        self.assertEqual(wakeup_count, 1)

    def test_rejected_result_does_not_record_wakeup(self):
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET status = 'failed' WHERE attempt_id = 'attempt-1'"
            )
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[{"type": "comment", "url": "https://github.com/x/y/issues/1#comment"}],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="done",
            used_skills=["fast-code-path"],
        )

        record = result.record_result(db_path, payload)

        self.assertFalse(record["ok"], record)
        self.assertEqual(record["status"], "rejected_by_supervisor")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            wakeup_count = conn.execute("SELECT COUNT(*) FROM wakeups").fetchone()[0]
            result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
        self.assertEqual(wakeup_count, 0)
        self.assertEqual(result_count, 0)

    def test_snapshot_rejects_task_attempt_mismatch(self):
        from robert_agent.worker import snapshot

        db_path = self._init_attempt_db()

        with self.assertRaisesRegex(ValueError, "attempt_id does not belong to task_id"):
            snapshot.record_snapshot(
                db_path=db_path,
                task_id="task-2",
                attempt_id="attempt-1",
                phase="verify",
                status="running",
                summary="wrong task",
            )

        with closing(sqlite3.connect(db_path)) as conn, conn:
            phase_count = conn.execute("SELECT COUNT(*) FROM worker_phases").fetchone()[0]
        self.assertEqual(phase_count, 0)

    def test_result_records_used_skills_metadata(self):
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[{"type": "comment", "url": "https://github.com/x/y/issues/1#comment"}],
            consumed_event_fingerprints=["comment:1"],
            verification=[{"command": "python -m unittest", "status": "passed"}],
            handoff="done",
            used_skills=["fast-code-path"],
            review_point_evaluation=[self._review_point("correct", "comment")],
        )

        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            metadata = conn.execute(
                "SELECT metadata_json FROM worker_results"
            ).fetchone()[0]
        self.assertEqual(
            json.loads(metadata)["used_skills"],
            ["fast-code-path"],
        )
        self.assertEqual(
            json.loads(metadata)["review_point_evaluation"],
            [self._review_point("correct", "comment")],
        )

    def test_result_records_memory_metadata(self):
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        memory_delta = {
            "status": "has_memory",
            "entries": [
                {
                    "operation": "upsert",
                    "kind": "decision",
                    "title": "Keep PR workstreams separate from issue workstreams",
                    "short_summary": "DD PR follow-up comments update the PR workstream.",
                    "long_summary": "The PR workstream remains linked to the origin issue, but it has its own active task mutex.",
                    "paths": ["src/robert_agent/run_once.py"],
                    "symbols": ["_create_task_attempt_and_prompt"],
                    "keywords": ["workstream", "dd-pr-followup"],
                    "confidence": "medium",
                }
            ],
        }
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[{"type": "comment", "url": "https://github.com/x/y/issues/1#comment"}],
            consumed_event_fingerprints=["comment:1"],
            verification=[{"command": "python -m unittest", "status": "passed"}],
            handoff="done",
            used_skills=["fast-code-path"],
            memory_delta=memory_delta,
            used_memory_ids=["pmem-existing"],
        )

        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            metadata = json.loads(
                conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0]
            )
        self.assertEqual(metadata["memory_delta"], memory_delta)
        self.assertEqual(metadata["used_memory_ids"], ["pmem-existing"])

    def test_result_records_default_memory_metadata(self):
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[{"type": "comment", "url": "https://github.com/x/y/issues/1#comment"}],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="done",
            used_skills=["fast-code-path"],
        )

        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            metadata = json.loads(
                conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0]
            )
        self.assertEqual(
            metadata["memory_delta"],
            {
                "status": "none",
                "reason": "worker did not provide reusable project memory",
            },
        )
        self.assertEqual(metadata["used_memory_ids"], [])

    def test_result_records_planned_actions_as_unpublished_intents(self):
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/x/y/issues/1",
                    "body": "Looks good",
                }
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[{"command": "python -m unittest", "status": "passed"}],
            handoff="ready to comment",
            used_skills=["fast-code-path"],
        )

        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            action = conn.execute(
                """
                SELECT action_type, target_url, external_id, audit_status, publish_status, metadata_json
                FROM github_actions
                """
            ).fetchone()
        self.assertEqual(action[:5], ("comment", "https://github.com/x/y/issues/1", None, "pending", "not_published"))
        self.assertEqual(json.loads(action[5])["body"], "Looks good")

    def test_result_rejects_missing_used_skills(self):
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        payload = {
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "output_type": "comment_analysis",
            "planned_github_actions": [
                {"type": "comment", "url": "https://github.com/x/y/issues/1#comment"}
            ],
            "consumed_event_fingerprints": ["comment:1"],
            "verification": [],
            "handoff": "missing skill evidence",
        }

        record = result.record_result(db_path, payload)

        self.assertFalse(record["ok"], record)
        self.assertEqual(record["status"], "failed_validation")
        self.assertIn("used_skills", record["safe_error"])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
            action_count = conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0]
        self.assertEqual(result_count, 0)
        self.assertEqual(action_count, 0)

    def test_result_rejects_legacy_actual_actions_key(self):
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        payload = {
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "output_type": "comment_analysis",
            "actual_github_actions": [
                {"type": "comment", "url": "https://github.com/x/y/issues/1#comment"}
            ],
            "consumed_event_fingerprints": ["comment:1"],
            "used_skills": ["fast-code-path"],
            "verification": [],
            "handoff": "legacy action payload",
        }

        record = result.record_result(db_path, payload)

        self.assertFalse(record["ok"], record)
        self.assertEqual(record["status"], "failed_validation")
        self.assertIn("planned_github_actions", record["safe_error"])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
            action_count = conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0]
        self.assertEqual(result_count, 0)
        self.assertEqual(action_count, 0)

    def test_stale_attempt_result_is_recorded_for_audit(self):
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET status = 'stale' WHERE attempt_id = 'attempt-1'"
            )

        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[
                {"type": "comment", "url": "https://github.com/x/y/issues/1#comment"}
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="recovered and completed",
            used_skills=["fast-code-path"],
        )
        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status = conn.execute(
                "SELECT status FROM attempts WHERE attempt_id = 'attempt-1'"
            ).fetchone()[0]
            result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
            action_count = conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0]
        self.assertEqual(attempt_status, "completed")
        self.assertEqual(result_count, 1)
        self.assertEqual(action_count, 1)

    def test_result_rejects_action_without_type(self):
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        payload = result.build_result(
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

        record = result.record_result(db_path, payload)

        self.assertFalse(record["ok"], record)
        self.assertEqual(record["status"], "failed_validation")
        self.assertIn("planned_github_actions[*].type", record["safe_error"])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
            action_count = conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0]
        self.assertEqual(result_count, 0)
        self.assertEqual(action_count, 0)

    def test_result_rejects_supervisor_failed_attempt_without_pending_actions(self):
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET status = 'failed', finished_at = ? WHERE attempt_id = 'attempt-1'",
                (datetime.now(timezone.utc).isoformat(),),
            )
            conn.execute(
                "UPDATE tasks SET lifecycle = 'failed' WHERE task_id = 'task-1'"
            )
            conn.execute(
                "UPDATE workstreams SET lifecycle = 'failed', active_task_id = NULL WHERE workstream_id = 'ws-1'"
            )

        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[
                {"type": "comment", "url": "https://github.com/x/y/issues/1#comment"}
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="late result",
            used_skills=["fast-code-path"],
        )
        record = result.record_result(db_path, payload)

        self.assertFalse(record["ok"], record)
        self.assertEqual(record["status"], "rejected_by_supervisor")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status = conn.execute(
                "SELECT status FROM attempts WHERE attempt_id = 'attempt-1'"
            ).fetchone()[0]
            result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
            action_count = conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0]
        self.assertEqual(attempt_status, "failed")
        self.assertEqual(result_count, 0)
        self.assertEqual(action_count, 0)

    def test_result_rejects_unknown_task_or_attempt(self):
        from robert_agent.worker import result
        from robert_agent import storage

        db_path = self.root / "dd.sqlite3"
        storage.init_database(db_path)

        payload = result.build_result(
            task_id="missing-task",
            attempt_id="missing-attempt",
            output_type="comment_analysis",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="done",
            used_skills=["fast-code-path"],
        )

        with self.assertRaises(sqlite3.IntegrityError):
            result.record_result(db_path, payload)

    def test_heartbeat_executes_command_and_records_phase(self):
        from robert_agent.worker import heartbeat

        db_path = self._init_attempt_db()

        command_result = heartbeat.run_command_with_heartbeat(
            db_path=db_path,
            task_id="task-1",
            attempt_id="attempt-1",
            phase="verify",
            command=[sys.executable, "-c", "import time; time.sleep(0.35); print('ok')"],
            timeout_seconds=5,
            heartbeat_interval_seconds=0.1,
        )

        self.assertTrue(command_result["ok"], command_result)
        self.assertEqual(command_result["returncode"], 0)
        self.assertNotIn("stdout", command_result)
        self.assertNotIn("stderr", command_result)
        self.assertIn("ok", command_result["stdout_tail"])
        self.assertGreater(command_result["stdout_bytes"], 0)
        self.assertTrue(Path(command_result["stdout_path"]).exists())
        self.assertTrue(Path(command_result["stderr_path"]).exists())
        with closing(sqlite3.connect(db_path)) as conn, conn:
            phase_count = conn.execute("SELECT COUNT(*) FROM worker_phases").fetchone()[0]
            summaries = [row[0] for row in conn.execute("SELECT summary FROM worker_phases")]
        self.assertGreaterEqual(phase_count, 3)
        self.assertTrue(any("still running" in summary for summary in summaries))

    def test_heartbeat_returns_only_tail_for_large_output(self):
        from robert_agent.worker import heartbeat

        db_path = self._init_attempt_db()

        command_result = heartbeat.run_command_with_heartbeat(
            db_path=db_path,
            task_id="task-1",
            attempt_id="attempt-1",
            phase="verify",
            command=[
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('A' * 200 + 'TAIL')",
            ],
            timeout_seconds=5,
            heartbeat_interval_seconds=0.1,
            tail_bytes=16,
            tail_lines=80,
        )

        self.assertTrue(command_result["ok"], command_result)
        self.assertNotIn("stdout", command_result)
        self.assertEqual(command_result["stdout_tail"], "AAAAAAAAAAAATAIL")
        self.assertTrue(command_result["stdout_truncated"])
        self.assertGreater(command_result["stdout_bytes"], len(command_result["stdout_tail"]))
        self.assertEqual(
            Path(command_result["stdout_path"]).read_text(encoding="utf-8"),
            "A" * 200 + "TAIL",
        )

    def _init_attempt_db(self):
        from robert_agent import storage

        db_path = self.root / "dd.sqlite3"
        storage.init_database(db_path)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root)
                VALUES ('repo-1', 'example/backend', 'robert-bot', 'master', '/repo', '/repo/.worktrees')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(workstream_id, repo_id, lifecycle, created_at, updated_at)
                VALUES ('ws-1', 'repo-1', 'active', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, created_at, updated_at)
                VALUES ('task-1', 'ws-1', 'running', 'P1', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, heartbeat_at)
                VALUES ('attempt-1', 'task-1', 1, 'running', ?, ?)
                """,
                (now, now),
            )
        return db_path

    def _dd_pr_body(self, text):
        return (
            "<!-- robert-workstream\n"
            "origin_workstream_id: github:x/y#1\n"
            "source_issue: 1\n"
            "task_id: task-1\n"
            "created_by: robert\n"
            "-->\n"
            + text
        )

    def _dd_comment_body(self, text):
        return "<!-- robert-comment task_id=task-1 attempt_id=attempt-1 event_fingerprints=comment:1 -->\n" + text

    def _review_response_action(self, text="Responded to the review report."):
        return {
            "type": "comment",
            "target_url": "https://github.com/x/y/pull/1#pullrequestreview-9",
            "body": self._dd_comment_body(text),
        }

    def _review_point(self, verdict, action):
        return {
            "summary": "The review point asks whether this change is needed.",
            "verdict": verdict,
            "reasoning": "Checked the current code path and compared it with the requested behavior.",
            "action": action,
        }

    def _verification_entry(self, status="passed"):
        entry = {
            "command": ["python3", "-B", "-m", "unittest"],
            "status": status,
            "purpose": "Verify the worker result contract.",
            "required": True,
        }
        if status != "skipped":
            entry["exit_code"] = 0 if status == "passed" else 1
        return entry


if __name__ == "__main__":
    unittest.main()
