"""Publication refuses forged or damaged single-pass evidence through its public gate."""

from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

from robert_agent.execution.stages import StageStopped
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.workflows.pre_pr_review.coordinator import Coordinator
from tests.support import database
from tests.support.pre_pr_review import PrePRReviewFixture


class SingleRoundProofTests(PrePRReviewFixture):
    def test_corrected_address_handoff_resumes_without_another_model_call(self):
        driver = self.start(["claude"], legacy=False)
        self.with_finding = True
        launch = driver.launcher

        def extra_field(**kwargs):
            result = launch(**kwargs)
            if Path(kwargs["prompt_path"]).name.startswith("address-"):
                path = Path(kwargs["log_dir"]) / "dispositions.json"
                response = json.loads(path.read_text())
                response["verification"][0]["unsupported_fixture_field"] = "extra"
                path.write_text(json.dumps(response))
            return result

        driver.launcher = extra_field
        with patch.object(store, "seal_result", side_effect=OSError("delivery checkpoint")):
            with self.assertRaisesRegex(OSError, "delivery checkpoint"):
                driver.run()
        before = self.launches.copy()
        resumed = Coordinator(self.db, driver.task_id, driver.attempt_id, launcher=extra_field, poll_seconds=0.01)
        with patch.object(store, "seal_result", side_effect=OSError("delivery checkpoint")):
            with self.assertRaisesRegex(OSError, "delivery checkpoint"):
                resumed.run()
        self.assertEqual(self.launches, before)
        with database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
            address = next(run for run in entry["completion_runs"] if run["stage"] == "address_findings")
        self.assertIn("handoff_repair_path", address["artifact"])
        corrected = Path(address["artifact"]["handoff_repair_path"])
        original_bytes = corrected.read_bytes()
        corrected.write_bytes(original_bytes + b" ")
        with self.assertRaisesRegex(StageStopped, "corrected handoff changed"):
            resumed.run()
        self.assertEqual(self.launches, before)
        corrected.write_bytes(original_bytes)
        self.assertTrue(resumed.run()["ok"])
        self.assertEqual(self.launches, before)

    def completed(self):
        driver = self.start(["claude"], legacy=False)
        self.with_finding = True
        self.assertTrue(driver.run()["ok"])
        with database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
        self.assertTrue(self.accepts(driver, entry))
        return driver, entry

    def accepts(self, driver, entry):
        return store.matches_submission(entry, driver.attempt_id, entry["proof"]["fingerprint"])

    def test_unbound_or_inconsistent_completion_cannot_release_submission(self):
        driver, entry = self.completed()
        for field, value in (
            ("snapshot_id", "other-snapshot"),
            ("candidate_sha", "c" * 40),
            ("final_sha", "d" * 40),
            ("report_sha256", "other-report"),
            ("dispositions_sha256", "other-dispositions"),
            ("address_run_id", "missing-run"),
            ("review_run_ids", []),
            ("version", True),
        ):
            changed = deepcopy(entry)
            changed["proof"]["single_round_completion"][field] = value
            with self.subTest(field=field):
                self.assertFalse(self.accepts(driver, changed))
        legacy = deepcopy(entry)
        legacy["request"]["plan"].pop("review_policy")
        self.assertFalse(self.accepts(driver, legacy))
        unknown = deepcopy(entry)
        unknown["request"]["plan"]["review_policy"] = "unlimited"
        self.assertFalse(self.accepts(driver, unknown))

    def test_missing_or_live_stage_evidence_cannot_release_submission(self):
        driver, entry = self.completed()
        missing = deepcopy(entry)
        missing["completion_runs"] = []
        self.assertFalse(self.accepts(driver, missing))
        for stage in ("review", "address_findings"):
            changed = deepcopy(entry)
            run = next(run for run in changed["completion_runs"] if run["stage"] == stage)
            run["slot_reserved"] = 1
            with self.subTest(stage=stage):
                self.assertFalse(self.accepts(driver, changed))
        duplicate = deepcopy(entry)
        extra = deepcopy(next(run for run in duplicate["completion_runs"] if run["stage"] == "address_findings"))
        extra["run_id"] = "another-completed-repair"
        duplicate["completion_runs"].append(extra)
        self.assertFalse(self.accepts(driver, duplicate))

    def test_damaged_dispositions_cannot_release_the_registered_result(self):
        driver, entry = self.completed()
        address = next(run for run in entry["completion_runs"] if run["stage"] == "address_findings")
        path = Path(address["artifact"]["dispositions_path"])
        path.write_bytes(path.read_bytes() + b" ")
        self.assertFalse(self.accepts(driver, entry))
