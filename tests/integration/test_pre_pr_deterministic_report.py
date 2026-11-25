from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

from robert_agent.adapters.git.review_revision import git
from robert_agent.delivery import queries
from robert_agent.execution.stages import StageStopped
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.workflows.pre_pr_review.coordinator import Coordinator
from tests.support import database as fixture_database
from tests.support.pre_pr_review import PrePRReviewFixture
from tests.support.repository_cycle import _verification_evidence


class DeterministicPrePRReportTests(PrePRReviewFixture):
    def test_clean_double_review_reuses_implementation_without_more_model_stages(self):
        driver = self.start(["codex", "claude"], legacy=False, legacy_report=False)
        skipped = _verification_evidence("optional-smoke", status="skipped", required=False)
        self.verification.append(skipped)

        result = driver.run()

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches, {"implement": 1, "reviewer-prompt.md": 2})
        with fixture_database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
            report = Path(entry["report_path"]).read_text()
            self.assertIn(entry["manifest"]["head_sha"], report)
            self.assertIn("codex", report)
            self.assertIn("claude", report)
            self.assertIn("覆盖范围", report)
            self.assertEqual(entry["proof"]["finding_ids"], [])
            self.assertEqual(entry["proof"]["candidate_sha"], entry["proof"]["head_sha"])
            self.assertEqual(queries.ready_actions(conn, 10), [])

        self.cycle([])
        with fixture_database.connect(self.db) as conn:
            actions = queries.ready_actions(conn, 10)
            self.assertEqual([row["action_type"] for row in actions], ["push_existing_pr", "open_pr"])
            (metadata,) = conn.execute("SELECT metadata_json FROM worker_results").fetchone()
            self.assertIn({**skipped, "attested": False}, json.loads(metadata)["normalized_payload"]["verification"])

    def test_no_repair_completion_rejects_damaged_implementation_or_receipt(self):
        driver = self.start(["claude"], legacy=False, legacy_report=False)
        result = driver.run()
        self.assertTrue(result["ok"], result)
        with fixture_database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
        fingerprint = entry["proof"]["fingerprint"]
        self.assertTrue(store.matches_submission(entry, driver.attempt_id, fingerprint))

        altered = deepcopy(entry)
        implementation = next(run for run in altered["completion_runs"] if run["stage"] == "implement")
        implementation["dispatch"]["result_checkpoint"]["worktree_head"] = "b" * 40
        self.assertFalse(store.matches_submission(altered, driver.attempt_id, fingerprint))

        receipt = Path(entry["manifest"]["artifact_root"]) / f"no-repair-{entry['report_sha256']}.json"
        receipt.write_bytes(receipt.read_bytes() + b" ")
        self.assertFalse(store.matches_submission(entry, driver.attempt_id, fingerprint))

    def test_material_judgment_enters_address_even_when_reviewers_find_no_defect(self):
        driver = self.start(["codex", "claude"], legacy=False, legacy_report=False)
        self.judgment_workers = {"claude"}

        result = driver.run()

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches, {"implement": 1, "reviewer-prompt.md": 2, "address": 1})
        with fixture_database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
            self.assertIn("待核查", Path(entry["report_path"]).read_text())

    def test_same_finding_from_both_reviewers_gets_one_bound_disposition(self):
        driver = self.start(["codex", "claude"], legacy=False, legacy_report=False)
        self.with_finding = True

        result = driver.run()

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches, {"implement": 1, "reviewer-prompt.md": 2, "address": 1})
        with fixture_database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
            (finding_id,) = entry["proof"]["finding_ids"]
            report = Path(entry["report_path"]).read_text()
            self.assertIn(f"### {finding_id}", report)
            self.assertIn("`claude` · `F1`", report)
            self.assertIn("`codex` · `F1`", report)
            self.assertNotEqual(entry["proof"]["candidate_sha"], entry["proof"]["head_sha"])
            self.assertEqual(entry["final_result_id"], result["result_id"])

    def test_related_base_change_stops_before_reusing_unverified_sha(self):
        driver = self.start(["claude"], legacy=False, legacy_report=False)
        launch = driver.launcher
        changed = []

        def advance_base(**kwargs):
            result = launch(**kwargs)
            if Path(kwargs["prompt_path"]).name == "reviewer-prompt.md" and not changed:
                root = self.git_fixture.upstream
                git(root, "checkout", "main")
                (root / "consumer.py").write_text("from source import value\nbase_feature = True\n")
                git(root, "add", "consumer.py")
                git(root, "commit", "-m", "related upstream change")
                changed.append(git(root, "rev-parse", "HEAD"))
            return result

        driver.launcher = advance_base
        with self.assertRaisesRegex(StageStopped, "base or original context changed"):
            driver.run()

        self.assertTrue(changed)
        self.assertEqual(self.launches, {"implement": 1, "reviewer-prompt.md": 1})
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(queries.ready_actions(conn, 10), [])
            count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
            self.assertEqual(count, 0)

    def test_resume_reuses_verification_after_ignored_configuration_changes_back(self):
        driver = self.start(["claude"], legacy=False, legacy_report=False)
        assigned = Path(driver.assigned_path)
        exclude = Path(git(assigned, "rev-parse", "--git-path", "info/exclude"))
        with exclude.open("a") as stream:
            stream.write("\n.fixture-config\n")
        config = assigned / ".fixture-config"
        config.write_text("pin=original\n")
        launch = driver.launcher

        def change_config_after_implementation(**kwargs):
            result = launch(**kwargs)
            if Path(kwargs["prompt_path"]).name == "reviewer-prompt.md":
                config.write_text("pin=changed\n")
            return result

        driver.launcher = change_config_after_implementation
        with patch.object(store, "seal_result", side_effect=OSError("delivery checkpoint")):
            with self.assertRaisesRegex(OSError, "delivery checkpoint"):
                driver.run()
        self.assertEqual(self.launches, {"implement": 1, "reviewer-prompt.md": 1, "address": 1})

        config.write_text("pin=original\n")
        resumed = Coordinator(
            self.db, driver.task_id, driver.attempt_id, launcher=self.launch_pr_stage, poll_seconds=0.01
        )
        result = resumed.run()

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches, {"implement": 1, "reviewer-prompt.md": 1, "address": 1})
