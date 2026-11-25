"""Publish a safe PR body while retaining the writer's sealed evidence."""

import json
from pathlib import Path

from robert_agent.execution.stages import StageStopped
from robert_agent.persistence.rounds import pre_pr_review as store
from tests.support import database
from tests.support.pre_pr_review import PrePRReviewFixture


class PublicBodyTests(PrePRReviewFixture):
    def body_driver(self, text):
        driver = self.start(["claude"])
        launch = driver.launcher
        originals = []

        def body(**kwargs):
            result = launch(**kwargs)
            if Path(kwargs["prompt_path"]).name.startswith("address-"):
                path = Path(kwargs["log_dir"]) / "dispositions.json"
                value = json.loads(path.read_text())
                value["body"] += "\n" + text
                path.write_text(json.dumps(value))
                originals.append((path, path.read_bytes()))
            return result

        driver.launcher = body
        return driver, originals

    def test_local_paths_get_public_copy_without_rerunning_or_mutating_handoff(self):
        driver, originals = self.body_driver("Validated in /data/workspace/example/backend; relevant checks passed.")
        result = driver.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches["address"], 1)
        self.assertEqual(originals[0][0].read_bytes(), originals[0][1])
        with database.connect(self.db) as conn:
            metadata = conn.execute("SELECT metadata_json FROM github_actions WHERE action_type='open_pr'").fetchone()[
                0
            ]
        body = json.loads(metadata)["body"]
        self.assertNotIn("/data/workspace/example/backend", body)
        self.assertIn("<local-path>", body)
        self.assertIn("relevant checks passed", body)

    def test_high_risk_secret_still_blocks_publication_and_preserves_handoff(self):
        driver, originals = self.body_driver("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789")
        with self.assertRaises(StageStopped):
            driver.run()
        self.assertEqual(originals[0][0].read_bytes(), originals[0][1])
        with database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0], 0)

    def test_historical_redaction_failure_gets_fresh_checks_from_repaired_head(self):
        driver, originals = self.body_driver("Validated in /data/workspace/example/backend.")
        self.with_finding = True
        validate = driver.validate_submission
        failed = []

        def reject_old(draft, **kwargs):
            if self.launches["address"] and not failed:
                failed.append(True)
                raise ValueError("Public text requires redaction before submission.")
            return validate(draft, **kwargs)

        driver.validate_submission = reject_old
        result = driver.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches["implement"], 1)
        self.assertEqual(self.launches["address"], 2)
        self.assertEqual(driver.verification_retry["error"], "Public text requires redaction before submission.")
        for path, raw in originals:
            self.assertEqual(path.read_bytes(), raw)
        with store.connect(self.db) as conn:
            original = next(r for r in store.runs(conn, driver.round_id) if r["stage"] == "address_findings")
        self.assertEqual(original["status"], "failed")
        self.assertEqual(original["error"], "Public text requires redaction before submission.")
