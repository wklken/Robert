"""A durable single-round policy delivers once with auditable unresolved findings."""

import json
from pathlib import Path
from unittest.mock import patch

from robert_agent.adapters.git.review_revision import git
from robert_agent.delivery import queries
from robert_agent.execution.stages import StageStopped
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.workflows.pre_pr_review.coordinator import Coordinator
from tests.support import database
from tests.support.pre_pr_review import PrePRReviewFixture


POLICY = "single_review_single_repair"


class SingleRoundReviewTests(PrePRReviewFixture):
    def single(self, workers=None):
        return self.start(workers or ["claude"], legacy=False)

    def completed_address(self, driver):
        with patch.object(driver, "finalize", side_effect=StageStopped("delivery checkpoint")):
            with self.assertRaisesRegex(StageStopped, "delivery checkpoint"):
                driver.run()
        with database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
            runs = store.runs(conn, driver.round_id)
        address = next(run for run in runs if run["stage"] == "address_findings")
        return entry, address

    def test_new_binding_selects_single_round_policy(self):
        driver = self.start(["claude"], legacy=False)
        self.assertEqual(driver.plan.get("review_policy"), POLICY)

    def test_fixed_findings_deliver_without_final_model_review(self):
        driver = self.single()
        self.with_finding = True
        result = driver.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches["reviewer-prompt.md"], 1)
        self.assertEqual(self.launches["address"], 1)
        self.assertEqual(self.launches["verify_final"], 0)
        with database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
            self.assertEqual(entry["proof"]["review_policy"], POLICY)
            self.assertNotIn("final_verification", entry["proof"])
            marker = entry["proof"]["single_round_completion"]
            self.assertEqual(marker["version"], 1)
            self.assertEqual(marker["final_sha"], entry["proof"]["head_sha"])
            self.assertEqual(marker["candidate_sha"], entry["proof"]["candidate_sha"])
            self.assertEqual(len(marker["review_run_ids"]), 1)
            self.assertIsNone(marker["synthesis_run_id"])
            self.assertTrue(store.matches_submission(entry, driver.attempt_id, entry["proof"]["fingerprint"]))

    def test_needs_decision_is_delivered_with_reason_and_all_checks(self):
        driver = self.single()
        self.with_finding = self.needs_decision = True
        self.question_choices = []
        result = driver.run()
        self.assertTrue(result["ok"], result)
        with database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
            self.assertEqual(entry["status"], "ready")
            row = conn.execute("SELECT metadata_json FROM github_actions WHERE action_type='open_pr'").fetchone()
            body = json.loads(row[0])["body"]
            self.assertIn("needs_decision", body)
            self.assertIn("Confirm expected value within the original scope.", body)
            self.assertIn("未执行独立的最终模型复核", body)
            self.assertNotIn("decision", entry["proof"])
        self.assertEqual(self.launches["address"], 1)
        self.assertEqual(self.launches["verify_final"], 0)

    def test_required_check_failure_still_blocks_single_round_delivery(self):
        driver = self.single()
        self.with_finding = self.needs_decision = True
        launch = driver.launcher

        def failed_check(**kwargs):
            result = launch(**kwargs)
            if Path(kwargs["prompt_path"]).name.startswith("address-"):
                path = Path(kwargs["log_dir"]) / "dispositions.json"
                value = json.loads(path.read_text())
                value["verification"][0].update(status="failed", exit_code=1)
                path.write_text(json.dumps(value))
            return result

        driver.launcher = failed_check
        with (
            patch.object(driver.stopping, "wait", return_value=False) as wait,
            self.assertRaisesRegex((StageStopped, ValueError), "required check"),
        ):
            driver.run()
        self.assertEqual([delay.args[0] for delay in wait.call_args_list if delay.args[0] >= 5], [5, 10])
        with database.connect(self.db) as conn:
            self.assertEqual(queries.ready_actions(conn, 10), [])

    def test_unrelated_base_advance_reuses_review_and_address(self):
        driver = self.single()
        self.with_finding = True
        launch = driver.launcher
        changed = []

        def advance(**kwargs):
            result = launch(**kwargs)
            if Path(kwargs["prompt_path"]).name.startswith("address-") and not changed:
                root = self.git_fixture.upstream
                git(root, "checkout", "main")
                (root / "docs").mkdir(exist_ok=True)
                (root / "docs/unrelated.md").write_text("Independent docs\n")
                git(root, "add", "docs/unrelated.md")
                git(root, "commit", "-m", "advance unrelated base")
                changed.append(git(root, "rev-parse", "HEAD"))
            return result

        driver.launcher = advance
        self.assertTrue(driver.run()["ok"])
        self.assertEqual(self.launches["address"], 1)
        self.assertEqual(self.launches["reviewer-prompt.md"], 1)
        self.assertEqual(self.launches["verify_final"], 0)
        with database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
            self.assertEqual(entry["proof"]["base_sha"], changed[0])
            self.assertEqual(entry["proof"]["reviewed_base_sha"], entry["manifest"]["base_sha"])

    def test_related_base_advance_stops_without_second_model_round(self):
        driver = self.single()
        self.with_finding = True
        launch = driver.launcher
        changed = []

        def advance(**kwargs):
            result = launch(**kwargs)
            if Path(kwargs["prompt_path"]).name.startswith("address-") and not changed:
                root = self.git_fixture.upstream
                git(root, "checkout", "main")
                (root / "consumer.py").write_text("from source import value\nbase_feature = True\n")
                git(root, "add", "consumer.py")
                git(root, "commit", "-m", "advance related base")
                changed.append(git(root, "rev-parse", "HEAD"))
            return result

        driver.launcher = advance
        with self.assertRaisesRegex(StageStopped, "base.*single-round"):
            driver.run()
        self.assertEqual(self.launches["address"], 1)
        self.assertEqual(self.launches["reviewer-prompt.md"], 1)
        self.assertEqual(self.launches["verify_final"], 0)
        with database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
            self.assertEqual(entry["status"], "attention")
            self.assertNotIn("base_merge", entry["request"])
            self.assertEqual(queries.ready_actions(conn, 10), [])

    def test_delivery_retry_reuses_completed_artifacts_with_same_policy(self):
        driver = self.single(["claude", "codex"])
        with patch.object(store, "seal_result", side_effect=OSError("handoff persistence unavailable")):
            with self.assertRaisesRegex(OSError, "handoff persistence unavailable"):
                driver.run()
        before = self.launches.copy()
        resumed = Coordinator(
            self.db, driver.task_id, driver.attempt_id, launcher=self.launch_pr_stage, poll_seconds=0.01
        )
        self.assertTrue(resumed.run()["ok"])
        self.assertEqual(self.launches, before)
        self.assertEqual(self.launches["verify_final"], 0)
        with database.connect(self.db) as conn:
            marker = store.session(conn, task_id=driver.task_id)["proof"]["single_round_completion"]
            self.assertEqual(len(marker["review_run_ids"]), 2)
            self.assertIsNotNone(marker["synthesis_run_id"])

    def test_all_deferred_findings_without_final_change_are_deliverable(self):
        driver = self.single()
        self.with_finding = True
        launch = driver.launcher

        def deferred(**kwargs):
            if Path(kwargs["prompt_path"]).name.startswith("address-"):
                self.with_finding = False
                result = launch(**kwargs)
                path = Path(kwargs["log_dir"]) / "dispositions.json"
                value = json.loads(path.read_text())
                value["dispositions"] = [{"id": "F1", "status": "deferred", "reason": "Outside original scope."}]
                path.write_text(json.dumps(value))
                return result
            return launch(**kwargs)

        driver.launcher = deferred
        self.assertTrue(driver.run()["ok"])
        with database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
            self.assertEqual(entry["proof"]["candidate_sha"], entry["proof"]["head_sha"])
            row = conn.execute("SELECT metadata_json FROM github_actions WHERE action_type='open_pr'").fetchone()
            self.assertIn("Outside original scope.", json.loads(row[0])["body"])
        self.assertEqual(self.launches["address"], 1)
        self.assertEqual(self.launches["verify_final"], 0)

    def test_needs_decision_missing_final_sha_cannot_be_delivered(self):
        driver = self.single()
        self.with_finding = self.needs_decision = True
        launch = driver.launcher

        def malformed(**kwargs):
            result = launch(**kwargs)
            if Path(kwargs["prompt_path"]).name.startswith("address-"):
                path = Path(kwargs["log_dir"]) / "dispositions.json"
                value = json.loads(path.read_text())
                value.pop("final_sha")
                path.write_text(json.dumps(value))
            return result

        driver.launcher = malformed
        with (
            patch.object(driver.stopping, "wait", return_value=False) as wait,
            self.assertRaisesRegex(ValueError, "final worktree HEAD"),
        ):
            driver.run()
        self.assertEqual([delay.args[0] for delay in wait.call_args_list if delay.args[0] >= 5], [5, 10])
        with database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM worker_results").fetchone()[0], 0)
        self.assertEqual(self.launches["verify_final"], 0)

    def test_completed_address_prompt_or_input_change_stops_without_relaunch(self):
        driver = self.single()
        entry, address = self.completed_address(driver)
        before = self.launches.copy()
        prompt = Path(address["dispatch"]["prompt_path"])
        changed = prompt.with_name("changed-address.md")
        changed.write_text(prompt.read_text() + "Changed request\n")
        for path, input_hash in ((changed, address["artifact"]["input_sha256"]), (prompt, "new-input")):
            with self.subTest(path=path, input_hash=input_hash):
                with self.assertRaisesRegex(StageStopped, "input changed"):
                    driver.run_phase("address_findings", driver.implementer, path, lambda *_: {}, input_hash=input_hash)
        self.assertEqual(self.launches, before)
        with database.connect(self.db) as conn:
            saved = next(run for run in store.runs(conn, driver.round_id) if run["run_id"] == address["run_id"])
            self.assertEqual(saved["status"], "succeeded")
            self.assertEqual(store.session(conn, task_id=driver.task_id)["snapshot_id"], entry["snapshot_id"])

    def test_damaged_completed_address_stops_and_preserves_success(self):
        driver = self.single()
        _, address = self.completed_address(driver)
        path = Path(address["artifact"]["dispositions_path"])
        value = json.loads(path.read_text())
        value["candidate_sha"] = "x" * 40
        path.write_text(json.dumps(value))
        before = self.launches.copy()
        with self.assertRaisesRegex(StageStopped, "completed single-round artifact changed"):
            driver.run()
        self.assertEqual(self.launches, before)
        with database.connect(self.db) as conn:
            saved = next(run for run in store.runs(conn, driver.round_id) if run["run_id"] == address["run_id"])
            self.assertEqual(saved["status"], "succeeded")

    def test_valid_but_changed_completed_address_stops_without_relaunch(self):
        driver = self.single()
        _, address = self.completed_address(driver)
        path = Path(address["artifact"]["dispositions_path"])
        path.write_bytes(path.read_bytes() + b" ")
        before = self.launches.copy()
        with self.assertRaisesRegex(StageStopped, "completed single-round artifact changed"):
            driver.run()
        self.assertEqual(self.launches, before)
        with database.connect(self.db) as conn:
            saved = next(run for run in store.runs(conn, driver.round_id) if run["run_id"] == address["run_id"])
            self.assertEqual(saved["status"], "succeeded")

    def test_report_renderer_change_reuses_original_report_and_address(self):
        from robert_agent.workflows.pre_pr_review import coordinator as module

        driver = self.single()
        entry, _ = self.completed_address(driver)
        before = self.launches.copy()
        with patch.object(module.report_checkpoint, "code_version", return_value="new-renderer-version"):
            self.assertTrue(driver.run()["ok"])
        self.assertEqual(self.launches, before)
        with database.connect(self.db) as conn:
            saved = store.session(conn, task_id=driver.task_id)
            self.assertEqual(
                (saved["report_path"], saved["report_sha256"]), (entry["report_path"], entry["report_sha256"])
            )

    def test_started_review_cannot_replace_original_context_or_snapshot(self):
        driver = self.single()
        with patch.object(driver, "address", side_effect=StageStopped("review checkpoint")):
            with self.assertRaisesRegex(StageStopped, "review checkpoint"):
                driver.run()
        before = self.launches.copy()
        original = driver.manifest["original_request"]
        driver.manifest["original_request"] = {**original, "sha256": "changed-context"}
        with self.assertRaisesRegex(
            (StageStopped, ValueError), "manifest changed|original request changed|original context changed"
        ):
            driver.prepare()
        driver.manifest["original_request"] = original
        with self.assertRaisesRegex(StageStopped, "single-round review started"):
            driver.save_candidate_snapshot(driver.manifest["candidate"], driver.manifest["base_sha"])
        self.assertEqual(self.launches, before)

    def test_partial_review_retries_only_missing_worker_on_same_snapshot(self):
        driver = self.single(["claude", "codex"])
        self.fail_worker = "codex"

        def checkpoint_wait(seconds):
            if seconds >= 5:
                raise StageStopped("partial review checkpoint")
            return False

        driver.stopping.wait = checkpoint_wait
        with self.assertRaisesRegex(StageStopped, "partial review checkpoint"):
            driver.run()
        before = self.launches.copy()
        snapshot = driver.manifest["snapshot_id"]
        with self.assertRaisesRegex(StageStopped, "single-round review started"):
            driver.save_candidate_snapshot(driver.manifest["candidate"], driver.manifest["base_sha"])
        self.fail_worker = None
        resumed = Coordinator(
            self.db, driver.task_id, driver.attempt_id, launcher=self.launch_pr_stage, poll_seconds=0.01
        )
        resumed.stopping.wait = lambda *_: False
        self.assertTrue(resumed.run()["ok"])
        self.assertEqual(resumed.manifest["snapshot_id"], snapshot)
        self.assertEqual(self.launches["reviewer-prompt.md"], before["reviewer-prompt.md"] + 1)
        self.assertEqual(self.launches["implement"], 1)
        self.assertEqual(self.launches["verify_final"], 0)
