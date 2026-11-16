"""Robert's TAPD comments are written once and found again by their visible receipt."""

from dataclasses import replace
import json
import unittest
from unittest import mock

from robert_agent.adapters.tapd import client as tapd_client
from robert_agent.delivery import contract
from robert_agent.delivery.application import audit, effects, reconcile, tapd_comments
from robert_agent.work import tapd_policy
from tests.support.tapd import STORY, FakeClient

NAME = "robert-wklken"
SETTINGS = {
    "enabled": True,
    "mention_name": NAME,
    "projects": [{"host": "tapd.example", "workspace_id": "100", "trusted_user": "alice", "label_rules": []}],
}
URL = f"https://tapd.example/100/prong/stories/view/{STORY}"
MARKER = contract.comment_marker("task-1", "attempt-1", 0)
SCOPE = {
    "repo": "example/repo",
    "sources": [
        {"source_type": "tapd_story", "provider": "tapd", "project_key": "tapd.example/100", "external_id": STORY}
    ],
}


def action(body=f"结论：已分析 @{NAME}\n\n{MARKER}", target_url=URL):
    return {
        "action_id": "action-1",
        "task_id": "task-1",
        "action_type": "comment",
        "target_url": target_url,
        "metadata_json": json.dumps({"body": body}),
    }


class TapdCommentTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.publisher = tapd_comments.Publisher(SETTINGS, "/unused", client=self.client)

    def publish(self, item=None, guard=None):
        return tapd_comments.publish(item or action(), self.publisher, MARKER, scope_guard=guard or (lambda: None))

    def reconcile(self):
        return reconcile.classify_remote_state(action(), SCOPE, None, tapd=self.publisher)

    def test_body_has_prefix_neutralized_mentions_and_a_final_receipt(self):
        body = tapd_comments.ticket_body(f"结论 @{NAME}\n\n{MARKER}", MARKER, NAME)
        parsed = tapd_policy.parse_comment(body, NAME)
        self.assertTrue(parsed.automatic)
        self.assertFalse(parsed.mentions)
        self.assertNotIn("robert-comment", body)
        self.assertTrue(body.endswith(f"回执 {tapd_comments.receipt_token(MARKER)}"))

    def test_publish_writes_once_and_a_replay_finds_the_receipt(self):
        first = self.publish()
        second = self.publish()
        self.assertEqual((first["status"], first["external_id"], first["target_url"]), ("published", "9001", URL))
        self.assertEqual((second["external_id"], second["deduplicated"]), ("9001", True))
        self.assertEqual(len(self.client.written), 1)

    def test_unknown_write_is_reconciled_by_receipt_instead_of_written_again(self):
        self.client.write_error = "timeout"
        failed = self.publish()
        self.assertEqual(failed["status"], "publish_failed")
        self.assertIsNot(failed["retryable"], False)
        found = self.reconcile()
        self.assertEqual((found["status"], found["evidence"]["external_id"]), ("confirmed_effect", "9001"))
        self.assertEqual(len(self.client.written), 1)

    def test_read_failures_prove_nothing_and_never_write(self):
        self.client.fail_reads = True
        self.assertEqual(self.publish()["safe_error"], "TAPD comment preflight failed")
        self.assertEqual(self.client.written, [])
        state = self.reconcile()
        self.assertEqual(state["reason_code"], "tapd_comment_query_failed")
        self.assertTrue(reconcile.read_failed(state))

    def test_unusable_ticket_comments_need_operator_evidence(self):
        for code in ("invalid_response", "duplicate_record", "output_limit", "scan_limit"):
            with (
                self.subTest(code=code),
                mock.patch.object(self.client, "ticket_comments", side_effect=tapd_client.TapdError(code)),
            ):
                state = self.reconcile()
                self.assertEqual(
                    (state["status"], state["reason_code"]), ("unknown", "tapd_comment_response_malformed")
                )
                self.assertFalse(reconcile.read_failed(state))

    def test_absent_receipt_and_conflicting_receipts(self):
        self.assertEqual(self.reconcile()["status"], "confirmed_absent")
        self.publish()
        self.client.comments.append(replace(self.client.comments[-1], comment_id="9999"))
        self.assertEqual(self.reconcile()["reason_code"], "comment_marker_conflict")

    def test_scope_guard_and_unconfigured_targets_stop_before_writing(self):
        blocked = {"ok": False, "status": "publish_failed", "safe_error": "stale"}
        self.assertIs(self.publish(guard=lambda: blocked), blocked)
        other = action(target_url="https://tapd.example/200/prong/stories/view/1100")
        self.assertIs(self.publish(other)["retryable"], False)
        self.assertEqual(self.client.written, [])

    def test_publish_action_routes_tapd_targets_without_github_commands(self):
        def no_github(*_args, **_kwargs):
            raise AssertionError("TAPD comments never run gh")

        result = effects.publish_action(action(), no_github, tapd=self.publisher)
        self.assertEqual(result["status"], "published")


class TapdActionScopeTests(unittest.TestCase):
    def test_targets_include_the_ticket_url(self):
        self.assertEqual(contract.target_urls(SCOPE), [URL])

    def test_tapd_task_may_comment_only_on_its_own_ticket(self):
        def comment(url):
            return [{"type": "comment", "target_url": url, "body": "x"}]

        self.assertIsNone(audit.action_scope_violation(comment(URL), SCOPE))
        self.assertIn("outside the workstream", audit.action_scope_violation(comment(URL.replace(STORY, "77")), SCOPE))
        github = audit.action_scope_violation(comment("https://github.com/example/repo/issues/1"), SCOPE)
        self.assertIn("own TAPD ticket", github)
        github_scope = {"repo": "example/repo", "sources": [{"source_type": "issue", "number": 1}]}
        self.assertIn("outside the workstream", audit.action_scope_violation(comment(URL), github_scope))

    def test_tapd_classification_cannot_recommend_development(self):
        def classify(route):
            return audit.audit_result(
                {
                    "output_type": "classification_result",
                    "recommended_route": route,
                    "branch_slug": "fix-login",
                    "consumed_event_fingerprints": ["tapd-comment:tapd.example/100/1"],
                    "planned_github_actions": [],
                },
                [],
                expected_output="classification_result",
                origin_type="tapd",
            )

        for route in ("vote", "review-pr", "review-comment"):
            self.assertIn("analysis, clarification or pull request", classify(route)["safe_error"])
        for route in ("comment-analysis", "waiting-for-user", "new-pr", "update-existing-pr"):
            self.assertTrue(classify(route)["ok"], route)


if __name__ == "__main__":
    unittest.main()
