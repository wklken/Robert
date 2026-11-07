"""Shared fixtures for compact result tests."""
import unittest
from tests.support import PACKAGE_ROOT  # noqa: F401
from tests.support import review_input_workflow as integration_tests


class CompactResultFixture(unittest.TestCase):
    setUp = integration_tests.ReviewInputIntegrationFixture.setUp

    prepare = integration_tests.ReviewInputIntegrationFixture.prepare

    def envelope(self, task, attempt, actions=None):
        return {"task_id": task, "attempt_id": attempt, "output_type": "update_existing_pr",
                "submission": {"planned_github_actions": actions or [{"type": "comment", "body": "Already correct."}],
                               "consumed_event_fingerprints": ["comment:request-1"], "used_skills": [],
                               "verification": [], "handoff": "Checked", "review_input_kind": "request"}}

    def settings(self):
        return {"origin_type": "github", "task_kind": "human_request", "action_scope": {
            "repo": "x/y", "workstream_id": "github:x/y#1", "branch_name": "codex/fix", "base_branch": "main",
            "worktree_path": "/worktree", "remote": "origin", "sources": [{"source_type": "issue", "number": 1}]}}
