"""TAPD-driven pull requests keep TAPD identity off GitHub and answer on the ticket."""

import unittest

from robert_agent.delivery import contract
from robert_agent.delivery.application import audit
from tests.support.tapd import STORY

TICKET = f"https://tapd.example/100/prong/stories/view/{STORY}"
SOURCE = {"source_type": "tapd_story", "provider": "tapd", "project_key": "tapd.example/100", "external_id": STORY}
SCOPE = {
    "workstream_id": f"tapd:tapd.example/100/story/{STORY}:example/repo",
    "public_ref": "rbt-0123456789ab",
    "repo": "example/repo",
    "base_branch": "main",
    "branch_name": "robert/rbt-0123456789ab-fix-login",
    "worktree_path": "/tmp/worktree",
    "remote": "origin",
    "sources": [SOURCE],
}
PUSH = {"type": "push_existing_pr"}
OPEN_PR = {"type": "open_pr", "title": "Fix login timeout", "body": "Retry the session refresh once."}
COMMENT = {"type": "comment", "body": "已修复登录超时，单元测试通过。"}
VERIFIED = {
    "command": "python -m unittest",
    "status": "passed",
    "purpose": "Verify the fix.",
    "required": True,
    "exit_code": 0,
    "source": "worker_reported",
}


def normalize(actions, output_type="new_pr"):
    envelope = {
        "task_id": "task-1",
        "attempt_id": "attempt-1",
        "output_type": output_type,
        "submission": {
            "planned_github_actions": [dict(action) for action in actions],
            "consumed_event_fingerprints": ["tapd-comment:tapd.example/100/1"],
            "verification": [VERIFIED],
            "handoff": "done",
            "used_skills": [],
        },
    }
    return contract.normalize(envelope, {"origin_type": "tapd", "action_scope": SCOPE, "task_kind": "human_request"})


def audit_types(actions, output_type="new_pr"):
    result = {
        "output_type": output_type,
        "consumed_event_fingerprints": ["tapd-comment:tapd.example/100/1"],
        "planned_github_actions": actions,
        "review_input_kind": "request",
        "verification": [VERIFIED],
    }
    return audit.audit_result(
        result,
        ["push_existing_pr", "open_pr", "comment"],
        expected_output=output_type,
        origin_type="tapd",
        review_input_policy="classify",
    )


class TapdPullRequestContractTests(unittest.TestCase):
    def test_backport_audit_rejects_other_target_base_and_original_branch(self):
        actions = normalize([PUSH, OPEN_PR, COMMENT])["planned_github_actions"]
        result = {
            "output_type": "new_pr",
            "planned_github_actions": actions,
            "consumed_event_fingerprints": ["tapd-comment:tapd.example/100/1"],
            "verification": [VERIFIED],
        }
        target_scope = {**SCOPE, "base_branch": "release/1.25", "branch_name": "codex/release-grants"}
        for name, value in (("base", "main"), ("head", SCOPE["branch_name"])):
            with self.subTest(field=name):
                actions[1].update(base="release/1.25", head="codex/release-grants")
                actions[0]["branch"] = "codex/release-grants"
                actions[1][name] = value
                checked = audit.audit_result(
                    result,
                    ["push_existing_pr", "open_pr", "comment"],
                    expected_output="new_pr",
                    origin_type="tapd",
                    action_scope=target_scope,
                )
                self.assertFalse(checked["ok"], checked)
                self.assertIn("outside", checked["safe_error"])

    def test_open_pr_marker_carries_only_the_public_reference(self):
        actions = normalize([PUSH, OPEN_PR, COMMENT])["planned_github_actions"]
        body = actions[1]["body"]
        self.assertIn("origin_workstream_id: rbt-0123456789ab", body)
        self.assertNotIn("tapd", body)
        self.assertNotIn(STORY, body)
        self.assertNotIn("Fixes", body)
        self.assertEqual(actions[2]["target_url"], TICKET)

    def test_pull_request_results_end_with_the_ticket_comment(self):
        marked = [
            {**action, "body": action.get("body", "")}
            for action in normalize([PUSH, OPEN_PR, COMMENT])["planned_github_actions"]
        ]
        self.assertTrue(audit_types(marked)["ok"], audit_types(marked))
        for actions in (marked[:2], [marked[2], *marked[:2]]):
            with self.subTest(order=[action["type"] for action in actions]):
                outcome = audit_types(actions)
                self.assertFalse(outcome["ok"])
                self.assertIn("comment on the TAPD ticket", outcome["safe_error"])

    def test_existing_pull_request_update_answers_on_the_ticket(self):
        actions = normalize([PUSH, COMMENT], "update_existing_pr")["planned_github_actions"]
        self.assertEqual(actions[1]["target_url"], TICKET)
        self.assertTrue(audit_types(actions, "update_existing_pr")["ok"])


if __name__ == "__main__":
    unittest.main()
