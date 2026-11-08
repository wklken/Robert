"""The compact submission owns semantics; Robert owns publication metadata."""

from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.delivery.application import submission as result
from robert_agent.execution import spool as result_spool
from robert_agent.delivery import contract as result_contract
from robert_agent.delivery.application import audit as audit_result
from tests.support.compact_result import CompactResultFixture


class CompactResultTests(CompactResultFixture):
    def test_classification_without_route_normalizes_to_waiting(self):
        for origin in ("github", "web"):
            for route in (None, "", " \n\t"):
                with self.subTest(origin=origin, route=route):
                    envelope = self.envelope("t", "a")
                    envelope["output_type"] = "classification_result"
                    submission = envelope["submission"]
                    submission.update(
                        planned_github_actions=[],
                        consumed_work_item_event_ids=["wie-1"],
                        handoff="Recommend route new-pr",
                    )
                    if route is not None:
                        submission["recommended_route"] = route
                    normalized = result_contract.normalize(envelope, {**self.settings(), "origin_type": origin})

                    self.assertEqual(normalized["recommended_route"], "waiting-for-user")
                    self.assertEqual(normalized["output_type"], "classification_result")
                    self.assertEqual(normalized["planned_github_actions"], [])
                    self.assertEqual(normalized["handoff"], "Recommend route new-pr")
                    self.assertEqual(submission.get("recommended_route"), route)

    def test_compact_comments_get_distinct_program_markers(self):
        db, task, attempt, _, _ = self.prepare("comment")
        receipt = result.record_result(
            db,
            self.envelope(
                task,
                attempt,
                [
                    {"type": "comment", "body": "First"},
                    {"type": "comment", "body": "Second"},
                ],
            ),
        )
        self.assertTrue(receipt["ok"], receipt)
        with closing(sqlite3.connect(db)) as conn:
            actions = [
                json.loads(row[0])
                for row in conn.execute("SELECT metadata_json FROM github_actions ORDER BY created_at")
            ]
        self.assertIn("action_index=0", actions[0]["body"])
        self.assertIn("action_index=1", actions[1]["body"])
        self.assertEqual(actions[0]["target_url"], "https://github.com/example/backend/pull/456")

    def test_mechanical_fields_are_rejected_without_reserving_a_slot(self):
        db, task, attempt, _, _ = self.prepare("comment")
        envelope = self.envelope(task, attempt)
        envelope["submission"]["planned_github_actions"][0]["repo"] = "example/backend"
        receipt = result.record_result(db, envelope)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["status"], "failed_validation")
        self.assertEqual(list(result_spool.directory(db).glob("*.json")), [])
        del envelope["submission"]["planned_github_actions"][0]["repo"]
        self.assertTrue(result.record_result(db, envelope)["ok"])

    def test_same_semantics_do_not_reenter_normalizer(self):
        db, task, attempt, _, _ = self.prepare("comment")
        envelope = self.envelope(task, attempt)
        first = result.record_result(db, envelope)
        self.assertTrue(first["ok"], first)
        with patch(
            "robert_agent.delivery.contract.normalize",
            side_effect=AssertionError("must reuse persisted result"),
        ):
            again = result.record_result(db, envelope)
        self.assertEqual(again["result_id"], first["result_id"])
        envelope["submission"]["handoff"] = "Different semantics"
        self.assertEqual(result.record_result(db, envelope)["status"], "result_conflict")

    def test_missing_attempt_contract_is_not_accepted(self):
        db, task, attempt, _, _ = self.prepare("comment")
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=json_remove(metadata_json,'$.result_contract')")
        self.assertEqual(
            result.record_result(db, self.envelope(task, attempt))["status"],
            "unsupported_contract",
        )

    def test_deferred_result_replays_saved_normalization(self):
        db, task, attempt, _, _ = self.prepare("comment")
        envelope = self.envelope(task, attempt)
        with patch.object(
            result,
            "_record_result_db",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            receipt = result.record_result(db, envelope)
        self.assertEqual(receipt["status"], "deferred", receipt)
        pending = list(result_spool.directory(db).glob("*.json"))
        self.assertEqual(len(pending), 1)
        saved = json.loads(pending[0].read_text())
        self.assertEqual(saved["result_contract"], result_contract.CONTRACT_ID)
        with patch.object(
            result_contract,
            "normalize",
            side_effect=AssertionError("must not normalize replay"),
        ):
            self.assertEqual(result.replay_pending_results(db)["recorded"], 1)
        with closing(sqlite3.connect(db)) as conn:
            metadata = json.loads(conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0])
        self.assertEqual(
            metadata["normalized_payload"]["planned_github_actions"],
            saved["payload"]["planned_github_actions"],
        )

    def test_pending_result_keeps_first_semantic_slot_and_cancellation_refuses_it(self):
        db, task, attempt, _, _ = self.prepare("comment")
        envelope = self.envelope(task, attempt)
        with patch.object(
            result,
            "_record_result_db",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            self.assertEqual(result.record_result(db, envelope)["status"], "deferred")
        changed = self.envelope(task, attempt)
        changed["submission"]["handoff"] = "different"
        self.assertEqual(result.record_result(db, changed)["status"], "result_conflict")
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle='canceled' WHERE task_id=?", (task,))
        self.assertEqual(result.record_result(db, envelope)["status"], "rejected_by_supervisor")
        self.assertEqual(list(result_spool.directory(db).glob("*.json")), [])

    def test_unknown_spool_format_is_preserved_and_not_replayed(self):
        db, task, attempt, _, _ = self.prepare("comment")
        with result_spool.locked_record(db, attempt) as path:
            result_spool.atomic_write(
                path,
                {
                    "status": "pending",
                    "payload": {"task_id": task, "attempt_id": attempt},
                },
            )
        self.assertEqual(result.replay_pending_results(db)["rejected"], 1)
        self.assertTrue(path.with_suffix(".invalid").exists())
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM worker_results").fetchone()[0], 0)

    def test_issue_resolution_is_explicit_and_program_generates_reference(self):
        for resolution, reference in (("complete", "Fixes #1"), ("partial", "Refs #1")):
            envelope = self.envelope(
                "t",
                "a",
                [
                    {"type": "push_existing_pr"},
                    {
                        "type": "open_pr",
                        "title": "Fix",
                        "body": "Implemented change.",
                        "issue_resolution": resolution,
                        "remaining_scope": "Follow-up",
                    },
                ],
            )
            envelope["output_type"] = "new_pr"
            normalized = result_contract.normalize(envelope, self.settings())
            self.assertEqual(normalized["planned_github_actions"][0]["branch"], "codex/fix")
            self.assertIn(reference, normalized["planned_github_actions"][1]["body"])
            self.assertEqual(normalized["planned_github_actions"][1]["repo"], "x/y")

    def test_inline_closing_and_url_references_are_rejected(self):
        for body in (
            "This fixes #99.",
            "- Closes x/y#2",
            "Fixes https://github.com/x/y/issues/2",
        ):
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
            with (
                self.subTest(body=body),
                self.assertRaises(result_contract.InvalidSubmission),
            ):
                result_contract.normalize(envelope, self.settings())

    def test_classification_cannot_skip_verification_shape_validation(self):
        submission = self.envelope("t", "a")["submission"]
        submission["verification"] = [
            {
                "source": "worker_reported",
                "command": {},
                "status": "bogus",
                "purpose": None,
                "required": "yes",
                "exit_code": 1,
            }
        ]
        with self.assertRaises(result_contract.InvalidSubmission):
            result_contract.validate_submission(submission, "classification_result", "github")

    def test_ambiguous_publication_target_requires_selection(self):
        settings = self.settings()
        settings["action_scope"]["sources"].append({"source_type": "pull_request", "number": 2})
        with self.assertRaises(result_contract.InvalidSubmission):
            result_contract.normalize(self.envelope("t", "a"), settings)
        envelope = self.envelope("t", "a")
        envelope["submission"]["planned_github_actions"][0]["target_url"] = "https://github.com/x/y/pull/2"
        self.assertEqual(
            result_contract.normalize(envelope, settings)["planned_github_actions"][0]["target_url"],
            "https://github.com/x/y/pull/2",
        )

    def test_output_cardinality_is_checked_even_with_broad_action_permissions(self):
        for output, types in (
            ("local_result", ["comment"]),
            ("classification_result", ["comment"]),
            ("pr_review_comment", ["comment", "comment"]),
            ("waiting_for_user", ["push_existing_pr", "comment"]),
        ):
            with self.subTest(output=output):
                self.assertIsNotNone(audit_result._audit_expected_actions(output, types))


if __name__ == "__main__":
    unittest.main()
