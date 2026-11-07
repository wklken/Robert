from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch
from tests.support import PACKAGE_ROOT  # noqa: F401
from tests.support import compact_result as compact_tests
from robert_agent.delivery import contract as result_contract
from robert_agent.execution import spool as result_spool
from robert_agent.delivery.application import submission as result


class SubmissionRemediationTests(unittest.TestCase):
    setUp = compact_tests.CompactResultFixture.setUp
    prepare = compact_tests.CompactResultFixture.prepare
    envelope = compact_tests.CompactResultFixture.envelope
    settings = compact_tests.CompactResultFixture.settings

    def test_target_errors_list_exact_allowed_urls(self):
        settings = self.settings()
        settings["action_scope"]["sources"].append({"source_type": "pull_request", "number": 2})
        expected = ["https://github.com/x/y/issues/1", "https://github.com/x/y/pull/2"]
        for target in (
            None,
            "https://github.com/x/y/pull/2#issuecomment-123",
            "https://github.com/other/repo/issues/1",
        ):
            envelope = self.envelope("t", "a")
            action = envelope["submission"]["planned_github_actions"][0]
            if target is not None:
                action["target_url"] = target
            with self.subTest(target=target):
                with self.assertRaises(result_contract.InvalidSubmission) as caught:
                    result_contract.normalize(envelope, settings)
                self.assertEqual(caught.exception.response().get("allowed_target_urls"), expected)
                # A candidate can be copied verbatim without guessing or normalization.
                action["target_url"] = expected[1]
                self.assertEqual(
                    result_contract.normalize(envelope, settings)["planned_github_actions"][0]["target_url"],
                    expected[1],
                )

    def test_no_allowed_target_diagnostic_is_explicit(self):
        settings = self.settings()
        settings["action_scope"]["sources"] = []
        with self.assertRaises(result_contract.InvalidSubmission) as caught:
            result_contract.normalize(self.envelope("t", "a"), settings)
        self.assertEqual(caught.exception.response().get("allowed_target_urls"), [])

    def test_database_error_after_durable_save_acknowledges_deferred_and_replays(self):
        db, task, attempt, _, _ = self.prepare("comment")
        envelope = self.envelope(task, attempt)
        with patch.object(
            result,
            "_record_result_db",
            side_effect=sqlite3.DatabaseError("disk I/O error"),
        ):
            receipt = result.record_result(db, envelope)
            self.assertTrue(receipt["ok"], receipt)
            self.assertEqual(receipt["status"], "deferred")
            pending = list(result_spool.directory(db).glob("*.json"))
            self.assertEqual(len(pending), 1)
            saved = pending[0].read_bytes()
            self.assertEqual(result.record_result(db, envelope)["status"], "deferred")
            self.assertEqual(pending[0].read_bytes(), saved)
            different = self.envelope(task, attempt)
            different["submission"]["handoff"] = "another result"
            self.assertEqual(result.record_result(db, different)["status"], "result_conflict")
        self.assertEqual(result.replay_pending_results(db)["recorded"], 1)
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM worker_results").fetchone()[0], 1)

    def test_database_error_before_save_is_not_acknowledged(self):
        db, task, attempt, _, _ = self.prepare("comment")
        with patch.object(result, "_prepare", side_effect=sqlite3.DatabaseError("disk I/O error")):
            receipt = result.record_result(db, self.envelope(task, attempt))
        self.assertFalse(receipt["ok"], receipt)
        self.assertEqual(receipt["status"], "validation_unavailable")
        self.assertEqual(list(result_spool.directory(db).glob("*.json")), [])

    def test_permanent_replay_database_error_still_exhausts(self):
        db, task, attempt, _, _ = self.prepare("comment")
        with patch.object(
            result,
            "_record_result_db",
            side_effect=sqlite3.DatabaseError("disk I/O error"),
        ):
            self.assertEqual(
                result.record_result(db, self.envelope(task, attempt))["status"],
                "deferred",
            )
            for _ in range(2):
                self.assertEqual(result.replay_pending_results(db)["deferred"], 1)
            self.assertEqual(result.replay_pending_results(db)["rejected"], 1)
        self.assertEqual(list(result_spool.directory(db).glob("*.json")), [])
        self.assertEqual(len(list((result_spool.directory(db) / "rejected").glob("*.json"))), 1)

    def test_pr_body_allows_documentation_refs_but_rejects_issue_directives(self):
        allowed = [
            "Refs: https://github.com/x/y/blob/main/README.md",
            "Refs: https://github.com/x/y/commit/abc123",
            "Refs: https://github.com/x/y",
            "Refs: https://github.com/x/y/issues",
        ]
        rejected = [
            "Refs: https://github.com/x/y/issues/99",
            "Fixes https://github.com/x/y/pull/99",
            "Resolves: x/y#99",
            "Refs #99",
            "Closes #99",
        ]
        for body in allowed + rejected:
            envelope = self.envelope(
                "t",
                "a",
                [
                    {
                        "type": "open_pr",
                        "title": "Fix",
                        "body": body,
                        "issue_resolution": "partial",
                        "remaining_scope": "Later",
                    }
                ],
            )
            envelope["output_type"] = "new_pr"
            with self.subTest(body=body):
                if body in rejected:
                    with self.assertRaises(result_contract.InvalidSubmission):
                        result_contract.normalize(envelope, self.settings())
                else:
                    normalized = result_contract.normalize(envelope, self.settings())
                    self.assertIn(body, normalized["planned_github_actions"][0]["body"])
                    self.assertIn("Refs #1", normalized["planned_github_actions"][0]["body"])
