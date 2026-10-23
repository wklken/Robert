from contextlib import closing
import json
import sys
import sqlite3
import tempfile
import unittest
from unittest import mock
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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

    def test_issue_sourced_new_pr_rejects_complete_resolution_with_refs(self):
        from robert_agent import audit_result

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
        from robert_agent import audit_result

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
        from robert_agent import audit_result

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
        from robert_agent import audit_result

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
        from robert_agent import audit_result

        audit = audit_result.audit_result(
            self._issue_new_pr_result(
                issue_resolution="complete",
                issue_reference="Fixes #1",
            ),
            allowed_github_actions=["push_existing_pr", "open_pr"],
            action_scope=self._issue_action_scope(),
        )

        self.assertEqual(audit["status"], "accepted")

    def test_issue_sourced_closing_reference_infers_complete_for_compatibility(self):
        from robert_agent import audit_result

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

    def test_blocked_execution_cannot_complete_with_waiting_route_recommendation(self):
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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
        from robert_agent import audit_result
        from robert_agent.worker import result

        payload = result.build_result(
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

    def test_dbos_owned_worker_result_queues_one_subject_signal(self):
        from unittest.mock import Mock, patch
        from robert_agent import durable_workflows, workflow_operations, workflow_signals, workflow_store
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            workflow_store.bind_subject(
                conn, "task", "task-1", "human_request", "dbos", "2026-08-02T00:00:00+00:00"
            )
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="done",
            used_skills=["fast-code-path"],
        )

        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn:
            rows = conn.execute(
                "SELECT workflow_id, topic, dedupe_key, payload_json FROM workflow_outbox"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0:3], ("robert:v1:task:task-1", "worker_result", record["result_id"]))
        self.assertEqual(json.loads(rows[0][3])["fact_id"], record["result_id"])

        messages = []
        relayed = workflow_signals.relay_due_signals(
            db_path,
            lambda _workflow_id, message, **_kwargs: messages.append(message),
            limit=10,
            now="2099-01-01T00:00:00+00:00",
        )
        self.assertEqual(relayed["delivered"], 1)
        driver = Mock()
        driver.next_transition.side_effect = [
            {"transition_id": "wait-1", "operation": "wait", "wait_seconds": 1,
             "terminal": False, "state": {}},
            {"transition_id": "terminal-1", "operation": "terminal", "wait_seconds": None,
             "terminal": True, "state": {"status": "canceled"}},
        ]
        driver.consume_signal.return_value = {
            "signal_id": messages[0]["signal_id"], "status": "consumed", "state": {},
        }
        driver.mark_attention.return_value = {"state": {}}
        with patch.object(workflow_operations, "current_human_request_driver", return_value=driver), \
             patch.object(durable_workflows.DBOS, "recv", return_value=messages[0]):
            outcome = durable_workflows.human_request_workflow_v1.__wrapped__("task-1")
        self.assertEqual(outcome["status"], "canceled", outcome)
        driver.consume_signal.assert_called_once_with("task-1", messages[0])
        driver.mark_attention.assert_not_called()
        repeated = workflow_signals.relay_due_signals(
            db_path,
            lambda _workflow_id, message, **_kwargs: messages.append(message),
            limit=10,
            now="2099-01-01T00:00:00+00:00",
        )
        self.assertEqual(repeated["delivered"], 0)
        self.assertEqual(len(messages), 1)

    def test_legacy_worker_result_does_not_queue_subject_signal(self):
        from robert_agent import workflow_store
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            workflow_store.bind_subject(
                conn, "task", "task-1", "human_request", "legacy", "2026-08-02T00:00:00+00:00"
            )
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="done",
            used_skills=["fast-code-path"],
        )

        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0],
                0,
            )

    def test_worker_result_and_subject_signal_roll_back_together(self):
        from robert_agent import workflow_signals, workflow_store
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            workflow_store.bind_subject(
                conn, "task", "task-1", "human_request", "dbos", "2026-08-02T00:00:00+00:00"
            )
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="done",
            used_skills=["fast-code-path"],
        )
        with mock.patch.object(
            workflow_signals,
            "record_subject_signal",
            side_effect=RuntimeError("forced signal failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced signal failure"):
                result.record_result(db_path, payload)

        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT status FROM attempts WHERE attempt_id = 'attempt-1'").fetchone()[0],
                "running",
            )

    def test_non_active_dbos_binding_rejects_worker_result_transaction(self):
        from robert_agent import workflow_store
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            workflow_store.bind_subject(
                conn, "task", "task-1", "human_request", "dbos", "2026-08-02T00:00:00+00:00"
            )
            conn.execute(
                """
                UPDATE workflow_bindings
                SET binding_status = 'attention'
                WHERE subject_type = 'task' AND subject_id = 'task-1'
                """
            )
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="done",
            used_skills=["fast-code-path"],
        )

        record = result.record_result(db_path, payload)

        self.assertFalse(record["ok"], record)
        self.assertEqual(record["status"], "rejected_by_workflow_owner")
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT status FROM attempts WHERE attempt_id = 'attempt-1'").fetchone()[0],
                "running",
            )

    def test_result_notifies_only_after_transaction_commits(self):
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="done",
            used_skills=["fast-code-path"],
        )
        observed = []

        def committed_notifier(notified_db_path):
            with closing(sqlite3.connect(notified_db_path)) as conn:
                observed.append(
                    (
                        conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0],
                        conn.execute("SELECT COUNT(*) FROM wakeups").fetchone()[0],
                        conn.execute(
                            "SELECT status FROM attempts WHERE attempt_id = 'attempt-1'"
                        ).fetchone()[0],
                    )
                )

        record = result.record_result(
            db_path,
            payload,
            notifier=committed_notifier,
        )

        self.assertTrue(record["ok"], record)
        self.assertEqual(observed, [(1, 1, "completed")])

    def test_result_stays_successful_when_post_commit_notification_fails(self):
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="done",
            used_skills=["fast-code-path"],
        )

        def failed_notifier(_db_path):
            raise OSError("listener unavailable")

        record = result.record_result(db_path, payload, notifier=failed_notifier)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM wakeups").fetchone()[0], 1)

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

    def test_result_records_remediation_evidence_metadata(self):
        from robert_agent.worker import result

        db_path = self._init_attempt_db()
        evidence = {
            "kind": "ci",
            "episode_id": "episode-ci",
            "observed_head_sha": "head-1",
            "observed_base_sha": "base-1",
            "failure_signature": "failure-1",
            "resolution_summary": "Adjusted the assertion and added coverage.",
        }
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="update_existing_pr",
            planned_github_actions=[],
            consumed_event_fingerprints=["github-system:workflow_run:101:1"],
            verification=[],
            handoff="fixed CI",
            used_skills=["fast-test-fix", "fast-preflight"],
            remediation_evidence=evidence,
        )

        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn:
            metadata = json.loads(
                conn.execute(
                    "SELECT metadata_json FROM worker_results"
                ).fetchone()[0]
            )
        self.assertEqual(metadata["remediation_evidence"], evidence)

    def test_remediation_audit_requires_matching_trusted_evidence_and_attestation(self):
        from robert_agent import audit_result
        from robert_agent.worker import result

        evidence = {
            "kind": "ci",
            "episode_id": "episode-ci",
            "observed_head_sha": "head-1",
            "observed_base_sha": "base-1",
            "failure_signature": "failure-1",
            "resolution_summary": "Fixed the failing unit test.",
        }
        payload = result.build_result(
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

    def test_heartbeat_preserves_command_result_when_snapshots_are_locked(self):
        from unittest.mock import patch
        from robert_agent.worker import heartbeat

        db_path = self._init_attempt_db()
        for locked_summary in ("Starting command", "Command still running", "Command exited 0"):
            with self.subTest(locked_summary=locked_summary):
                original = heartbeat.snapshot.record_snapshot

                def record_snapshot(**kwargs):
                    if kwargs["summary"] == locked_summary:
                        raise sqlite3.OperationalError("database is locked")
                    return original(**kwargs)

                with patch.object(heartbeat.snapshot, "record_snapshot", side_effect=record_snapshot):
                    try:
                        result = heartbeat.run_command_with_heartbeat(
                            db_path, "task-1", "attempt-1", "verify",
                            [sys.executable, "-c", "import time; time.sleep(.25); print('delivered')"],
                            timeout_seconds=5, heartbeat_interval_seconds=.1,
                        )
                    except sqlite3.OperationalError as exc:
                        self.fail(f"SQLite contention must not lose command completion: {exc}")
                self.assertEqual(result["returncode"], 0)
                self.assertEqual(result["stdout_tail"].strip(), "delivered")
                record = json.loads(Path(result["command_record_path"]).read_text())
                self.assertEqual(record["status"], "completed")
                self.assertEqual(record["returncode"], 0)
                self.assertEqual(record["attempt_id"], "attempt-1")
                if locked_summary == "Command exited 0":
                    self.assertEqual(result["snapshot_status"], "deferred")

    def test_heartbeat_journals_completion_before_database_write(self):
        from unittest.mock import patch
        from robert_agent.worker import heartbeat

        db_path = self._init_attempt_db()
        original = heartbeat.snapshot.record_snapshot
        observed = []

        def record_snapshot(**kwargs):
            if kwargs["summary"] == "Command exited 7":
                records = list((self.root / "artifacts" / "task-1").glob("*.command.json"))
                self.assertEqual(len(records), 1, "Persist exit evidence before the SQLite write")
                record = json.loads(records[0].read_text())
                self.assertEqual(record["returncode"], 7)
                self.assertEqual(record["status"], "failed")
                observed.append(record)
                raise sqlite3.OperationalError("database is locked")
            return original(**kwargs)

        with patch.object(heartbeat.snapshot, "record_snapshot", side_effect=record_snapshot):
            result = heartbeat.run_command_with_heartbeat(
                db_path, "task-1", "attempt-1", "verify",
                [sys.executable, "-c", "import sys; print('failed evidence'); sys.exit(7)"],
                heartbeat_interval_seconds=.1,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["returncode"], 7)
        self.assertEqual(result["snapshot_status"], "deferred")
        self.assertEqual(len(observed), 1)

    def test_heartbeat_timeout_has_durable_terminal_record(self):
        from robert_agent.worker import heartbeat

        result = heartbeat.run_command_with_heartbeat(
            self._init_attempt_db(), "task-1", "attempt-1", "verify",
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_seconds=.15, heartbeat_interval_seconds=.1,
        )
        self.assertFalse(result["ok"])
        self.assertIsNone(result["returncode"])
        record = json.loads(Path(result["command_record_path"]).read_text())
        self.assertTrue(record["timed_out"])
        self.assertEqual(record["status"], "failed")

    def test_heartbeat_completes_under_real_sqlite_writer_lock(self):
        import threading
        from robert_agent.worker import heartbeat

        db_path = self._init_attempt_db()
        with closing(sqlite3.connect(db_path, check_same_thread=False)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            release = threading.Timer(.2, writer.rollback)
            release.start()
            self.addCleanup(release.join)
            result = heartbeat.run_command_with_heartbeat(
                db_path, "task-1", "attempt-1", "verify",
                [sys.executable, "-c", "print('real contention')"],
                heartbeat_interval_seconds=.1,
            )
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["snapshot_status"], "recorded")
        self.assertIn("real contention", result["stdout_tail"])

    def test_heartbeat_invalid_attempt_does_not_launch_command_when_sqlite_locked(self):
        from unittest.mock import patch
        from robert_agent.worker import heartbeat

        db_path = self._init_attempt_db()
        marker = self.root / "unauthorized-command"
        with patch.object(heartbeat.snapshot, "record_snapshot", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(ValueError):
                heartbeat.run_command_with_heartbeat(
                    db_path, "wrong-task", "attempt-1", "verify",
                    [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                    heartbeat_interval_seconds=.1,
                )
        self.assertFalse(marker.exists())

    def test_heartbeat_unexpected_snapshot_error_reaps_child_and_records_failure(self):
        from unittest.mock import patch
        from robert_agent.worker import heartbeat

        db_path = self._init_attempt_db()
        original = heartbeat.snapshot.record_snapshot

        def record_snapshot(**kwargs):
            if kwargs["summary"] == "Command still running":
                raise sqlite3.OperationalError("no such table: worker_phases")
            return original(**kwargs)

        with patch.object(heartbeat.snapshot, "record_snapshot", side_effect=record_snapshot):
            with self.assertRaises(sqlite3.OperationalError):
                heartbeat.run_command_with_heartbeat(
                    db_path, "task-1", "attempt-1", "verify",
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    heartbeat_interval_seconds=.1,
                )
        record = json.loads(next((self.root / "artifacts" / "task-1").glob("*.command.json")).read_text())
        self.assertEqual(record["status"], "failed")
        self.assertIsNotNone(record["returncode"])

    @unittest.skipUnless(sys.platform != "win32", "POSIX command process groups")
    def test_heartbeat_timeout_reaps_command_descendants(self):
        import os
        import signal
        from robert_agent import run_once
        from robert_agent.worker import heartbeat

        pid_path = self.root / "descendant.pid"
        child_script = "import time; time.sleep(30)"
        script = (
            "import subprocess,sys,time; from pathlib import Path; "
            f"p=subprocess.Popen([sys.executable,'-c',{child_script!r}]); "
            f"Path({str(pid_path)!r}).write_text(str(p.pid)); time.sleep(30)"
        )
        result = heartbeat.run_command_with_heartbeat(
            self._init_attempt_db(), "task-1", "attempt-1", "verify",
            [sys.executable, "-c", script], timeout_seconds=.3, heartbeat_interval_seconds=.1,
        )
        pid = int(pid_path.read_text())
        try:
            self.assertEqual(run_once._command_process_status(pid, ""), "not_found")
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.assertFalse(result["ok"])

    def test_heartbeat_keeps_commands_in_worker_process_group(self):
        import os
        from robert_agent.worker import heartbeat

        result = heartbeat.run_command_with_heartbeat(
            self._init_attempt_db(), "task-1", "attempt-1", "verify",
            [sys.executable, "-c", "import os; print(os.getpgrp())"],
            heartbeat_interval_seconds=.1,
        )
        self.assertEqual(int(result["stdout_tail"]), os.getpgrp(),
                         "Worker cancellation must still clean up the whole command group")

    def test_custom_command_output_keeps_journal_discoverable_by_supervisor(self):
        from robert_agent import run_once
        from robert_agent.worker import heartbeat

        db_path = self._init_attempt_db()
        result = heartbeat.run_command_with_heartbeat(
            db_path, "task-1", "attempt-1", "verify", [sys.executable, "-c", "print('custom')"],
            output_dir=self.root / "external-output", heartbeat_interval_seconds=.1,
        )
        self.assertEqual(Path(result["stdout_path"]).parent, self.root / "external-output")
        self.assertEqual(Path(result["command_record_path"]).parent, self.root / "artifacts" / "task-1")
        with closing(sqlite3.connect(db_path)) as conn:
            recovery = run_once._inspect_attempt_recovery(conn, "attempt-1", "task-1", None)
        self.assertEqual(recovery["commands"][0]["returncode"], 0)

    def test_command_cleanup_reaps_parent_when_process_inspection_fails(self):
        import subprocess
        from unittest.mock import patch
        from robert_agent.worker import heartbeat

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        with patch.object(heartbeat.subprocess, "run", side_effect=OSError("ps unavailable")):
            try:
                heartbeat._kill_command(proc)
            except OSError:
                pass
            self.assertIsNotNone(proc.poll(), "Inspection failure must not skip reaping the command")

    def test_command_start_failure_has_terminal_journal(self):
        from robert_agent.worker import heartbeat

        db_path = self._init_attempt_db()
        with self.assertRaises(FileNotFoundError):
            heartbeat.run_command_with_heartbeat(
                db_path, "task-1", "attempt-1", "verify", [str(self.root / "missing-executable")],
            )
        record = json.loads(next((self.root / "artifacts" / "task-1").glob("*.command.json")).read_text())
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["summary"], "Command failed to start: FileNotFoundError")

    def test_replay_after_completion_snapshot_does_not_duplicate_phase(self):
        from robert_agent import run_once
        from robert_agent.worker import heartbeat

        db_path = self._init_attempt_db()
        result = heartbeat.run_command_with_heartbeat(
            db_path, "task-1", "attempt-1", "verify", [sys.executable, "-c", "print('done')"],
            heartbeat_interval_seconds=.1,
        )
        path = Path(result["command_record_path"])
        record = json.loads(path.read_text())
        # Simulate wrapper death after SQLite commit but before marking the journal recorded.
        record["snapshot_status"] = "deferred"
        path.write_text(json.dumps(record))
        with closing(sqlite3.connect(db_path)) as conn, conn:
            run_once._inspect_attempt_recovery(conn, "attempt-1", "task-1", None)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM worker_phases WHERE summary='Command exited 0'",
            ).fetchone()[0], 1)

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

    def _issue_action_scope(self):
        return {
            "repo": "x/y",
            "base_branch": "master",
            "worktree_path": "/repo/.worktrees/dd-1",
            "branch_name": "codex/dd-1",
            "sources": [{"source_type": "issue", "number": 1}],
        }

    def _issue_new_pr_result(
        self,
        *,
        issue_resolution,
        issue_reference,
        remaining_scope="",
    ):
        from robert_agent.worker import result

        open_pr = {
            "type": "open_pr",
            "repo": "x/y",
            "head": "codex/dd-1",
            "base": "master",
            "title": "Implement issue",
            "body": self._dd_pr_body(f"{issue_reference}\n\nImplementation details."),
            "issue_resolution": issue_resolution,
        }
        if remaining_scope:
            open_pr["remaining_scope"] = remaining_scope
        return result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="new_pr",
            planned_github_actions=[
                {
                    "type": "push_existing_pr",
                    "worktree_path": "/repo/.worktrees/dd-1",
                    "branch": "codex/dd-1",
                },
                open_pr,
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="opened pr",
            used_skills=[],
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
