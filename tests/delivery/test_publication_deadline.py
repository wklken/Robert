"""Deadline exhaustion must retain publication authority for a later lane."""

import unittest
from unittest import mock

from robert_agent.delivery.application import effects, intents
from robert_agent.foundation.commands import ControlDeadlineExceeded


class PublicationDeadlineTests(unittest.TestCase):
    def test_source_sha_deadline_is_not_missing_scope(self):
        runner = mock.Mock(side_effect=ControlDeadlineExceeded(["git", "rev-parse"], 0))
        with self.assertRaises(ControlDeadlineExceeded):
            intents.derive_push_source_sha({"action_type": "push_existing_pr"}, {"worktree_path": "/unused"}, runner)

    def test_head_owner_deadline_is_not_missing_scope(self):
        runner = mock.Mock(side_effect=ControlDeadlineExceeded(["git", "remote"], 0))
        with mock.patch.object(effects, "sibling_push_metadata", return_value={"worktree_path": "/unused"}):
            with self.assertRaises(ControlDeadlineExceeded):
                effects.trusted_head_owner_for_intent(None, {"metadata_json": "{}"}, {"repo": "x/y"}, runner)

    def test_reconciliation_deadline_preserves_executing_intent_and_budget(self):
        import sqlite3
        from contextlib import closing
        from tests.support.publication_reconcile import PublicationReconcileFixture, Completed
        from robert_agent.delivery.application import publish

        fixture = PublicationReconcileFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)

        def crash_after_write(command, **kwargs):
            if command[2].endswith("comments?per_page=100"):
                return Completed("[]")
            raise RuntimeError("lost write response")

        with self.assertRaises(RuntimeError):
            publish.publish_ready_actions(fixture.db_path, run_command=crash_after_write)
        with closing(sqlite3.connect(fixture.db_path)) as conn:
            before = conn.execute("SELECT metadata_json FROM github_actions").fetchone()[0]
        with self.assertRaises(ControlDeadlineExceeded):
            publish.publish_ready_actions(
                fixture.db_path, run_command=mock.Mock(side_effect=ControlDeadlineExceeded(["gh", "api"], 0))
            )
        with closing(sqlite3.connect(fixture.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM publication_intents").fetchone()[0], "executing")
            self.assertEqual(conn.execute("SELECT metadata_json FROM github_actions").fetchone()[0], before)

    def test_reconcile_queries_propagate_deadline(self):
        import json
        import subprocess
        from robert_agent.delivery.application import reconcile

        scope = {
            "repo": "x/y",
            "branch_name": "topic",
            "base_branch": "main",
            "head_owner": "x",
            "worktree_path": "/unused",
            "result_head_sha": "a" * 40,
            "existing_pr_head_sha": "b" * 40,
            "pr_number": 1,
            "observed_head_sha": "b" * 40,
            "observed_base_sha": "c" * 40,
            "task_kind": "ci_remediation",
        }
        action = {
            "action_type": "open_pr",
            "metadata_json": json.dumps({"body": "<!-- robert-workstream task_id=t -->"}),
        }
        deadline = ControlDeadlineExceeded(["gh", "api"], 0)
        candidate = {"number": 1, "baseRefName": "main", "headRefName": "topic", "headRepositoryOwner": {"login": "x"}}
        cases = [
            (action, [deadline]),
            (action, [subprocess.CompletedProcess([], 0, json.dumps([candidate]), ""), deadline]),
            ({"action_type": "push_existing_pr"}, [deadline]),
            (
                {"action_type": "push_existing_pr"},
                [subprocess.CompletedProcess([], 0, "b" * 40 + "\trefs/heads/topic\n", ""), deadline],
            ),
        ]
        for item, responses in cases:
            with self.subTest(action=item["action_type"], call_count=len(responses)):
                with self.assertRaises(ControlDeadlineExceeded):
                    reconcile.classify_remote_state(item, scope, mock.Mock(side_effect=responses))

    def test_maintenance_push_deadline_propagates(self):
        scope = {"supervision_repair": True, "result_head_sha": "a" * 40}
        with (
            mock.patch.object(effects.publication_scope, "trusted_action_scope", return_value=scope),
            mock.patch.object(effects.supervision_git, "push", side_effect=ControlDeadlineExceeded(["git", "push"], 0)),
        ):
            with self.assertRaises(ControlDeadlineExceeded):
                effects.publish_push_existing_pr(
                    {
                        "action_type": "push_existing_pr",
                        "metadata_json": '{"worktree_path":"/unused","branch":"topic"}',
                    },
                    mock.Mock(),
                    None,
                )
