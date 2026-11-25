"""Rejected final gates get fresh evidence without rewriting old handoffs."""

from copy import deepcopy
import json
from pathlib import Path

from robert_agent.adapters.git.review_revision import git
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.execution.stages import StageStopped
from robert_agent.workflows.pre_pr_review import verification_retry
from tests.support.pre_pr_review import PrePRReviewFixture


class VerificationRetryTests(PrePRReviewFixture):
    def test_question_with_old_incomplete_environment_pin_is_refreshed_without_selecting_an_answer(self):
        driver = self.start(["claude"])
        self.with_finding = True
        self.needs_decision = True
        original_register = driver.register
        original = {}

        def register_old_pin(run, artifact):
            result = original_register(run, artifact)
            if run["stage"] == "address_findings" and not original:
                with store.connect(self.db) as conn:
                    saved = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == run["run_id"])
                    dispatch = deepcopy(saved["dispatch"])
                    checkpoint = dispatch["result_checkpoint"]
                    checkpoint.pop("untracked_sha256")
                    checkpoint["source_pin_error"] = "stage source has too many untracked files to pin"
                    store.update_run(conn, run["run_id"], dispatch_json=json.dumps(dispatch))
                    original.update(run_id=run["run_id"], dispatch=dispatch, path=artifact["dispositions_path"])
                original["bytes"] = Path(original["path"]).read_bytes()
            return result

        driver.register = register_old_pin
        with self.assertRaisesRegex(StageStopped, "question source worktree changed"):
            driver.run()
        driver.register = original_register
        result = driver.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches["implement"], 1)
        self.assertEqual(self.launches["address"], 2)
        self.assertEqual(Path(original["path"]).read_bytes(), original["bytes"])
        with store.connect(self.db) as conn:
            saved = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == original["run_id"])
            entry = store.session(conn, task_id=driver.task_id)
        self.assertEqual(saved["dispatch"], original["dispatch"])
        self.assertEqual(entry["status"], "waiting_for_user")

    def test_single_round_gate_failure_recovers_the_repaired_source_without_another_review(self):
        driver = self.start(["claude"], legacy=False)
        self.with_finding = True
        launch = driver.launcher
        prompts = []
        repaired_head = []

        def fail_first_checks(**kwargs):
            prompt = Path(kwargs["prompt_path"])
            if prompt.name.startswith("address-") and prompts:
                self.assertIn("verification-retry", prompt.read_text())
                self.assertEqual(git(kwargs["worktree_path"], "rev-parse", "HEAD"), repaired_head[0])
            result = launch(**kwargs)
            if prompt.name.startswith("address-"):
                prompts.append(prompt)
                if len(prompts) == 1:
                    path = Path(kwargs["log_dir"]) / "dispositions.json"
                    value = json.loads(path.read_text())
                    repaired_head.append(value["final_sha"])
                    failed = deepcopy(value["verification"][0])
                    failed.update(status="failed", exit_code=2)
                    value["verification"].insert(0, failed)
                    path.write_text(json.dumps(value))
            return result

        driver.launcher = fail_first_checks
        driver.prepare()
        report, report_sha, ids = driver.report(driver.review())
        with self.assertRaisesRegex(ValueError, "required check"):
            driver.address(report, report_sha, ids)
        disposition = driver.address(report, report_sha, ids)
        self.assertTrue(driver.finalize(report, report_sha, disposition)["ok"])
        self.assertEqual(len(prompts), 2)
        self.assertNotEqual(prompts[0], prompts[1])
        self.assertEqual(self.launches["reviewer-prompt.md"], 1)
        self.assertEqual(self.launches["verify_final"], 0)

    def test_required_failure_retries_from_repaired_head_with_pinned_feedback(self):
        driver = self.start(["claude"])
        self.with_finding = True
        launch = driver.launcher
        rejected = {}

        def fail_first_checks(**kwargs):
            prompt = Path(kwargs["prompt_path"])
            if prompt.name.startswith("address-") and self.launches["address"]:
                self.assertIn("verification-retry", prompt.read_text())
                self.assertEqual(git(kwargs["worktree_path"], "rev-parse", "HEAD"), rejected["final_sha"])
                with store.connect(self.db) as conn:
                    rejected["run"] = next(
                        r for r in store.runs(conn, driver.round_id) if r["stage"] == "address_findings"
                    )
            result = launch(**kwargs)
            if prompt.name.startswith("address-") and self.launches["address"] == 1:
                path = Path(kwargs["log_dir"]) / "dispositions.json"
                value = json.loads(path.read_text())
                failed = deepcopy(value["verification"][0])
                failed.update(status="failed", exit_code=2, purpose="Intermediate required tests before repair.")
                value["verification"].insert(0, failed)
                path.write_text(json.dumps(value))
                rejected.update(value)
                rejected["path"] = path
                rejected["bytes"] = path.read_bytes()
            return result

        driver.launcher = fail_first_checks
        result = driver.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches["implement"], 1)
        self.assertEqual(self.launches["address"], 2)
        self.assertEqual(rejected["path"].read_bytes(), rejected["bytes"])
        self.assertEqual(verification_retry.inputs(driver)["input_sha256"], driver.verification_retry["input_sha256"])
        self.assertEqual(self.launches["address"], 2)
        verification_retry.verify(driver.verification_retry)
        source = Path(driver.verification_retry["handoff_path"])
        contents = source.read_bytes()
        source.write_text("changed rejected evidence")
        with self.assertRaisesRegex(StageStopped, "evidence changed"):
            verification_retry.verify(driver.verification_retry)
        source.write_bytes(contents)
        source_tree = Path(driver.verification_retry["worktree_path"])
        (source_tree / "source.py").write_text("changed repaired source\n")
        with self.assertRaises(ValueError):
            verification_retry.verify(driver.verification_retry)
        (source_tree / "source.py").write_text("value = 4\n")
        original = rejected["run"]
        with store.connect(self.db) as conn:
            saved = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == original["run_id"])
        for key in ("status", "error", "dispatch", "prompt_sha256", "output_dir"):
            self.assertEqual(saved[key], original[key])
