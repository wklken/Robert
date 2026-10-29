"""Correctable submissions must not consume an attempt's only result slot."""
from contextlib import closing
import json
import sqlite3
import unittest
from unittest import mock

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import result_spool, audit_result
from robert_agent.worker import result
from tests import test_review_input_workflow as integration_tests


class PreSubmitAuditTests(unittest.TestCase):
    setUp = integration_tests.ReviewInputIntegrationTests.setUp
    prepare = integration_tests.ReviewInputIntegrationTests.prepare

    def waiting(self, task_id, attempt_id, actions):
        return result.build_result(
            task_id, attempt_id, "waiting_for_user", ["comment:request-1"], [], "Need a decision",
            planned_github_actions=actions,
            operator_question={"kind": "clarification", "summary": "Which behavior is intended?"},
        )

    def test_waiting_missing_comment_can_be_corrected_in_same_attempt(self):
        db, task, attempt, _, _ = self.prepare("comment")
        rejected = result.record_result(db, self.waiting(task, attempt, []))
        self.assertFalse(rejected["ok"], rejected)
        self.assertEqual(rejected["status"], "failed_validation")
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM worker_results").fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT json_extract(metadata_json,'$.result_validation') FROM attempts WHERE attempt_id=?", (attempt,)).fetchone()[0])
        self.assertEqual(list(result_spool.directory(db).glob("*.json")), [])
        body = "Which behavior is intended?"
        accepted = result.record_result(db, self.waiting(task, attempt, [{"type": "comment", "target_url": "https://github.com/example/backend/pull/456", "body": body}]))
        self.assertTrue(accepted["ok"], accepted)

    def test_invalid_publication_target_does_not_reserve_final_result(self):
        db, task, attempt, _, _ = self.prepare("comment")
        body = "Question"
        payload = self.waiting(task, attempt, [{"type": "comment", "target_url": "https://github.com/other/repo/pull/456", "body": body}])
        response = result.record_result(db, payload)
        self.assertFalse(response["ok"], response)
        self.assertEqual(response["status"], "failed_validation")
        self.assertIn("reason_code", response)
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM github_actions").fetchone()[0], 0)
            counts = json.loads(conn.execute("SELECT json_extract(metadata_json,'$.submission_validation_counts') FROM attempts WHERE attempt_id=?", (attempt,)).fetchone()[0])
            self.assertEqual(counts, {"action_scope": 1})
        payload["submission"]["planned_github_actions"][0]["target_url"] = "https://github.com/example/backend/pull/456"
        self.assertTrue(result.record_result(db, payload)["ok"])

    def test_missing_question_is_rejected_before_any_durable_submission(self):
        db, task, attempt, _, _ = self.prepare("comment")
        body = "Question"
        payload = self.waiting(task, attempt, [{"type": "comment", "target_url": "https://github.com/example/backend/pull/456", "body": body}])
        payload["submission"].pop("operator_question")
        with mock.patch.object(result, "_record_result_db", side_effect=AssertionError("Invalid input reached durable submission")):
            rejected = result.record_result(db, payload)
        self.assertEqual(rejected["status"], "failed_validation")
        self.assertEqual(rejected["field_path"], "operator_question")
        self.assertEqual(list(result_spool.directory(db).glob("*.json")), [])

    def test_locked_validation_state_is_retryable_without_spool_slot(self):
        db, task, attempt, _, _ = self.prepare("comment")
        with closing(sqlite3.connect(db)) as lock:
            lock.execute("PRAGMA journal_mode=DELETE")
            lock.execute("BEGIN EXCLUSIVE")
            rejected = result.record_result(db, self.waiting(task, attempt, []))
        self.assertEqual(rejected["status"], "validation_unavailable")
        self.assertTrue(rejected["retryable"])
        self.assertEqual(list(result_spool.directory(db).glob("*.json")), [])

    def test_canceled_task_is_not_a_correctable_submission(self):
        db, task, attempt, _, _ = self.prepare("comment")
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle='canceled' WHERE task_id=?", (task,))
        rejected = result.record_result(db, self.waiting(task, attempt, []))
        self.assertEqual(rejected["status"], "rejected_by_supervisor")
        self.assertEqual(list(result_spool.directory(db).glob("*.json")), [])

    def test_waiting_remediation_final_audit_does_not_require_an_attestation(self):
        payload = self.waiting("task", "attempt", [{"type": "comment", "target_url": "https://github.com/example/backend/pull/456", "body": "<!-- robert-comment task_id=task attempt_id=attempt action_index=0 -->\nQuestion"}])
        for task_kind in ("ci_remediation", "merge_conflict_remediation"):
            with self.subTest(task_kind=task_kind):
                checked = audit_result.audit_result({key: value for key, value in payload.items() if key != "submission"} | payload["submission"], ["comment"], task_kind=task_kind)
                self.assertEqual(checked["status"], "accepted", checked)


if __name__ == "__main__":
    unittest.main()
