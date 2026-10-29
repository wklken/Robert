from contextlib import closing
import json
from pathlib import Path
import sqlite3
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import audit_result, render_prompt, route
from robert_agent.worker import result
from tests import test_run_once as run_tests


class ReviewInputWorkflowTests(unittest.TestCase):
    def payload(self, kind=None):
        payload = result.build_result(
            "task-1", "attempt-1", "update_existing_pr", ["comment:1"], [], "Handled request",
            planned_github_actions=[
                {"type": "push_existing_pr", "worktree_path": "/worktree", "branch": "codex/fix"},
                {"type": "comment", "target_url": "https://github.com/x/y/pull/1",
                 "body": "<!-- robert-comment task_id=task-1 attempt_id=attempt-1 event_fingerprints=comment:1 -->\nUpdated."},
            ],
        )
        if kind is not None:
            payload["review_input_kind"] = kind
        return payload

    def audit(self, payload, **kwargs):
        return audit_result.audit_result(payload, ["push_existing_pr", "comment"], **kwargs)

    def test_trigger_type_selects_policy_without_reading_body_keywords(self):
        for event_type, policy in (("review", "required"), ("review_comment", "required"),
                                   ("comment", "classify"), ("manual_operator_request", "classify"),
                                   ("unknown", "required")):
            with self.subTest(event_type=event_type):
                self.assertEqual(route.review_input_policy("update_existing_pr", {"event_type": event_type}), policy)
        self.assertEqual(route.review_input_policy("new_pr", {"event_type": "review"}), "none")
        self.assertEqual(route.review_input_policy("update_existing_pr", {"task_kind": "ci_remediation"}), "none")

    def test_ordinary_request_can_push_without_inventing_review_points(self):
        self.assertEqual(self.audit(self.payload("request"), review_input_policy="classify")["status"], "accepted")

    def test_native_review_cannot_opt_out_and_legacy_remains_strict(self):
        payload = self.payload("request")
        payload["review_input_policy"] = "classify"
        for kwargs in ({}, {"review_input_policy": "required"}):
            with self.subTest(kwargs=kwargs):
                audit = self.audit(payload, **kwargs)
                self.assertIn("missing_review_point_evaluation", audit["violations"])

    def test_report_in_ordinary_comment_requires_review_evidence(self):
        audit = self.audit(self.payload("review_feedback"), review_input_policy="classify")
        self.assertIn("missing_review_point_evaluation", audit["violations"])

    def test_missing_classification_and_invalid_values_do_not_bypass(self):
        for kind in (None, "", "direct", False):
            with self.subTest(kind=kind):
                self.assertEqual(self.audit(self.payload(kind), review_input_policy="classify")["status"], "policy_violation")

    def test_request_label_does_not_hide_rejected_review_points(self):
        payload = self.payload("request")
        payload["review_point_evaluation"] = [{"summary": "claim", "verdict": "incorrect", "reasoning": "counterevidence", "action": "skip"}]
        self.assertIn("no_implemented_review_points", self.audit(payload, review_input_policy="classify")["violations"])

    def test_mixed_request_requires_evaluation_but_can_fix_separate_request(self):
        payload = self.payload("mixed")
        self.assertIn("missing_review_point_evaluation", self.audit(payload, review_input_policy="classify")["violations"])
        payload["review_point_evaluation"] = [{"summary": "claim", "verdict": "incorrect", "reasoning": "counterevidence", "action": "skip"}]
        self.assertEqual(self.audit(payload, review_input_policy="classify")["status"], "accepted")
        self.assertIn("no_implemented_review_points", self.audit(payload, review_input_policy="required")["violations"])

    def test_conditional_prompt_has_explicit_branch_and_result_classification(self):
        prompt = render_prompt.render_prompt(
            {"task_id": "task-1", "attempt_id": "attempt-1", "workstream_id": "github:x/y!1", "review_input_policy": "classify"},
            route.ROUTES["update-existing-pr"], [{"event_type": "comment", "body": "Add a null check"}],
        )
        self.assertIn("review_input_kind", prompt)
        self.assertIn("request", prompt)
        self.assertIn("review_feedback", prompt)
        self.assertIn("Do not invent review points", prompt)
        self.assertIn("If the request includes both", prompt)


class ReviewInputIntegrationTests(unittest.TestCase):
    setUp = run_tests.RunOnceDryRunTests.setUp

    def prepare(self, event_type):
        from robert_agent import run_once

        self.fixture_path.write_text(json.dumps({"events": [{
            "id": "request-1", "number": 456, "source_type": "pull_request",
            "event_type": event_type, "actor_login": "wklken",
            "body": "@robert-bot add a null check", "intent": "small_change",
            "has_open_dd_pr": True, "existing_pr_head_branch": "codex/fix",
            "existing_pr_head_sha": "base-sha", "pr_author_login": "robert-bot",
            "url": "https://github.com/example/backend/pull/456",
        }]}))
        run = run_once.run_once(self.config_path, fixture_path=self.fixture_path, dry_run=True, skip_external=True)
        self.assertTrue(run["ok"], run)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn:
            task_id, attempt_id, metadata = conn.execute("SELECT t.task_id,a.attempt_id,t.metadata_json FROM tasks t JOIN attempts a ON a.task_id=t.task_id").fetchone()
            prompt_path = conn.execute("SELECT path FROM artifacts WHERE task_id=? AND artifact_type='prompt'", (task_id,)).fetchone()[0]
        return db_path, task_id, attempt_id, json.loads(metadata), Path(prompt_path).read_text()

    def test_comment_policy_survives_record_reload_and_controller_audit(self):
        from robert_agent import run_once

        db_path, task_id, attempt_id, metadata, prompt = self.prepare("comment")
        self.assertEqual(metadata["review_input_policy"], "classify")
        self.assertIn('review_input_kind="request"', prompt)
        payload = result.build_result(task_id, attempt_id, "update_existing_pr", ["comment:request-1"], [], "No change needed",
            planned_github_actions=[{"type": "comment", "target_url": "https://github.com/example/backend/pull/456",
                                     "body": f"<!-- robert-comment task_id={task_id} attempt_id={attempt_id} event_fingerprints=comment:request-1 -->\nThe requested check already exists."}],
            review_input_kind="request")
        record = result.record_result(db_path, payload)
        self.assertTrue(record["ok"], record)
        run_once.run_once(self.config_path, dry_run=True, skip_external=True)
        with closing(sqlite3.connect(db_path)) as conn:
            saved = json.loads(conn.execute("SELECT metadata_json FROM worker_results WHERE result_id=?", (record["result_id"],)).fetchone()[0])
        self.assertEqual(saved["review_input_kind"], "request")
        self.assertEqual(saved["audit"]["status"], "accepted")

    def test_native_review_policy_is_persisted_independent_of_request_text(self):
        _, _, _, metadata, prompt = self.prepare("review_comment")
        self.assertEqual(metadata["review_input_policy"], "required")
        self.assertIn("Review-point evaluation (required for this task)", prompt)
        self.assertNotIn('review_input_kind="request"', prompt)


if __name__ == "__main__":
    unittest.main()
