"""Unchanged findings and question handoffs retain the pre-PR release gates."""

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from robert_agent.adapters.git.review_revision import clean_head
from robert_agent.execution.stages import StageStopped
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.workflows.pre_pr_review import coordinator, final_verification
from tests.support.review_workspace import ReviewWorkspaceFixture


class UnchangedFindingVerificationTests(ReviewWorkspaceFixture):
    def setUp(self):
        super().setUp()
        self.report = self.root / "report.md"
        self.report.write_text("F1: inspect the retained behavior before accepting a deferral.\n")
        self.dispositions = self.root / "dispositions.json"
        self.dispositions.write_text(
            json.dumps(
                {
                    "candidate_sha": self.head,
                    "final_sha": self.head,
                    "report_sha256": store.digest(self.report),
                    "dispositions": [{"id": "F1", "status": "deferred", "reason": "Advisory behavior outside scope."}],
                }
            )
        )
        self.output = self.root / "final-output"
        self.output.mkdir()
        self.driver = SimpleNamespace(
            manifest={"head_sha": self.head, "snapshot_id": "snapshot"},
            round_id="round",
            original_request=None,
            scope={},
            request={"workers": [{"name": "independent-reviewer"}]},
            phase=Mock(),
            address_workspace=Mock(return_value=self.repo),
            prompt=self.write_prompt,
            run_phase=Mock(side_effect=self.review_output),
        )
        self.disposition = {
            "head": self.head,
            "run_id": "address-run",
            "dispositions_path": str(self.dispositions),
            "dispositions_sha256": store.digest(self.dispositions),
        }
        self.blocked = False

    def write_prompt(self, name, content, **kwargs):
        path = self.root / name
        path.write_text(content)
        return path

    def review_output(self, stage, worker, prompt, validate, *, input_hash):
        inputs = self.driver.final_review
        self.assertEqual(stage, "verify_final")
        self.assertEqual(worker["name"], "independent-reviewer")
        self.assertEqual(inputs["finding_ids"], ["F1"])
        self.assertEqual(inputs["dispositions"][0]["status"], "deferred")
        self.assertEqual(Path(inputs["additional_diff_path"]).read_text(), "")
        self.assertIn("nonblocking", Path(prompt).read_text())
        value = {
            key: inputs[key]
            for key in (
                "round_id",
                "snapshot_id",
                "candidate_sha",
                "final_sha",
                "report_sha256",
                "dispositions_sha256",
                "additional_diff_sha256",
            )
        }
        value.update(
            schema_version=1,
            recommendation="blocked" if self.blocked else "approved",
            blocking_findings=["F1 remains a blocker in the original scope"] if self.blocked else [],
            checked_findings=[
                {
                    "id": "F1",
                    "status": "blocking" if self.blocked else "deferred",
                    "reason": "Inspected retained source and original scope independently.",
                }
            ],
        )
        (self.output / "final-verification.json").write_text(json.dumps(value))
        return {**validate(self.output, "reviewer-model"), "input_sha256": input_hash, "run_id": "verifier-run"}

    def verify(self, findings):
        return final_verification.verify(
            self.driver, self.report, store.digest(self.report), findings, self.disposition
        )

    def test_deferred_finding_without_code_changes_requires_independent_review(self):
        artifact = self.verify(["F1"])
        self.driver.run_phase.assert_called_once()
        self.assertEqual(artifact["candidate_sha"], artifact["final_sha"])
        self.assertEqual(artifact["final_verification_sha256"], store.digest(artifact["final_verification_path"]))
        self.assertEqual(clean_head(self.repo), self.head)

    def test_independent_blocker_stops_unchanged_deferred_candidate(self):
        self.blocked = True
        with self.assertRaises(final_verification.FinalReviewBlocked) as caught:
            self.verify(["F1"])
        self.driver.run_phase.assert_called_once()
        self.assertEqual(caught.exception.value["checked_findings"][0]["status"], "blocking")
        self.assertEqual(clean_head(self.repo), self.head)

    def test_unchanged_candidate_without_findings_skips_extra_review(self):
        self.assertIsNone(self.verify([]))
        self.driver.run_phase.assert_not_called()


class QuestionRecoveryBoundaryTests(unittest.TestCase):
    def driver(self, *, final_blocker=False):
        decision = coordinator.ReviewDecisionRequired(Path("dispositions.json"), {"kind": "scope_decision"})
        driver = SimpleNamespace(
            db_path="unused-fixture-db",
            task_id="task",
            attempt_id="attempt",
            round_id="round",
            stage="address_findings",
            deadline=datetime.now(timezone.utc) + timedelta(minutes=1),
            reconcile_running=Mock(),
            prepare=Mock(),
            review=Mock(return_value=[]),
            report=Mock(return_value=(Path("report.md"), "report-hash", ["F1"])),
            address=Mock(return_value={}) if final_blocker else Mock(side_effect=decision),
            finalize=Mock(side_effect=final_verification.FinalReviewBlocked({}, {}, {})),
            check_active=Mock(),
            check_base=Mock(side_effect=coordinator.BaseRefreshRequired("new-base")),
        )
        outcome = {"ok": True, "status": "waiting_for_user"}
        driver.refresh_base = Mock()

        driver.wait_for_decision = MethodType(coordinator.Coordinator.wait_for_decision, driver)
        driver.advance_stages = MethodType(coordinator.Coordinator.advance_stages, driver)
        return driver, outcome, decision

    def test_primary_or_final_question_does_not_require_base_refresh(self):
        for final_blocker in (False, True):
            with self.subTest(final_blocker=final_blocker):
                driver, outcome, decision = self.driver(final_blocker=final_blocker)
                with (
                    patch.object(coordinator.final_repair, "handle", return_value=(decision.path, decision.question)),
                    patch.object(coordinator.decisions, "submit", return_value=outcome) as submit,
                ):
                    self.assertEqual(coordinator.Coordinator.run_stages(driver), outcome)
                submit.assert_called_once()
                driver.check_base.assert_not_called()
                driver.refresh_base.assert_not_called()
                self.assertEqual(driver.address.call_count, 1)

    def assert_attention(self, driver, reason):
        with (
            patch.object(coordinator.store, "connect", side_effect=lambda _: nullcontext(None)),
            patch.object(coordinator.store, "runs", return_value=[]),
            patch.object(coordinator.store, "update") as update,
            patch.object(coordinator.transactions, "begin"),
            patch.object(coordinator, "attempt_is_active", return_value=True),
            patch.object(coordinator, "terminate_children", return_value="stopped"),
        ):
            with self.assertRaisesRegex((StageStopped, ValueError), reason):
                coordinator.Coordinator.run_stages(driver)
        update.assert_called_once()
        self.assertEqual(update.call_args.kwargs["status"], "attention")
        self.assertIn(reason, update.call_args.kwargs["error"])

    def test_question_source_failure_is_recorded_as_attention(self):
        driver, _, _ = self.driver()
        with patch.object(coordinator.decisions, "submit", side_effect=StageStopped("question source changed")):
            self.assert_attention(driver, "question source changed")
        driver.refresh_base.assert_not_called()

    def test_question_validation_failure_is_recorded_as_attention(self):
        driver, _, _ = self.driver()
        with patch.object(coordinator.decisions, "submit", side_effect=ValueError("question not deliverable")):
            self.assert_attention(driver, "question not deliverable")
        driver.refresh_base.assert_not_called()


class UnchangedFindingProofTests(unittest.TestCase):
    def entry(self, findings):
        return {
            "status": "ready",
            "snapshot_id": "snapshot",
            "report_sha256": "report-hash",
            "proof": {
                "attempt_id": "attempt",
                "snapshot_id": "snapshot",
                "report_sha256": "report-hash",
                "candidate_sha": "a" * 40,
                "head_sha": "a" * 40,
                "finding_ids": findings,
                "fingerprint": "result-fingerprint",
            },
        }

    def test_unchanged_finding_proof_without_verifier_cannot_authorize_submission(self):
        self.assertFalse(store.matches_submission(self.entry(["F1"]), "attempt", "result-fingerprint"))

    def test_unchanged_proof_without_findings_can_authorize_submission(self):
        self.assertTrue(store.matches_submission(self.entry([]), "attempt", "result-fingerprint"))
