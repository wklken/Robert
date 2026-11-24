"""Recover validated stage work without erasing the original failed invocation."""

import json
from pathlib import Path
from unittest.mock import patch

from robert_agent.execution.stages import StageStopped
from robert_agent.persistence.rounds import review as store
from tests.support import database
from tests.support.review_coordinator import ReviewCoordinatorFixture


class StageArtifactAdoptionTests(ReviewCoordinatorFixture):
    def failed_registration(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        driver.request["workers"] = driver.request["workers"][:1]
        with patch.object(driver, "register", side_effect=ValueError("registration checkpoint unavailable")):
            with self.assertRaisesRegex(RuntimeError, "registration checkpoint unavailable"):
                driver.review()
        with database.connect(self.db) as conn:
            run = [r for r in store.runs(conn, driver.round_id) if r["stage"] == "review"][0]
        self.assertEqual(run["status"], "failed")
        return driver, run

    def test_registration_failure_adopts_sealed_result_with_no_new_model(self):
        driver, original = self.failed_registration()
        count = len(self.calls)
        records = driver.review()
        self.assertEqual(len(self.calls), count)
        self.assertEqual(records[0]["run_id"], original["run_id"])
        with database.connect(self.db) as conn:
            saved = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == original["run_id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["error"], original["error"])
        self.assertEqual(saved["dispatch"]["artifact_adoption"]["original_status"], "failed")
        self.assertEqual(saved["dispatch"]["artifact_adoption"]["original_error"], original["error"])
        self.assertTrue(saved["artifact"])
        driver.review()
        self.assertEqual(len(self.calls), count)

    def test_interrupted_complete_result_adopts_without_relaunch(self):
        driver, original = self.failed_registration()
        with database.connect(self.db) as conn:
            store.update_run(conn, original["run_id"], status="interrupted", error="coordinator stopped")
        count = len(self.calls)
        self.assertEqual(driver.review()[0]["run_id"], original["run_id"])
        self.assertEqual(len(self.calls), count)

    def test_interrupted_complete_result_without_exit_code_is_independently_adopted(self):
        driver, original = self.failed_registration()
        with database.connect(self.db) as conn:
            recorded = dict(original["dispatch"])
            recorded.pop("returncode", None)
            recorded["failure_kind"] = "interrupted"
            store.update_run(
                conn,
                original["run_id"],
                status="interrupted",
                error="controller stopped",
                dispatch_json=json.dumps(recorded),
            )
        count = len(self.calls)
        self.assertEqual(driver.review()[0]["run_id"], original["run_id"])
        self.assertEqual(len(self.calls), count)
        with database.connect(self.db) as conn:
            adopted = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == original["run_id"])
        self.assertEqual(adopted["status"], "interrupted")
        self.assertIn("artifact_adoption", adopted["dispatch"])

    def test_tampered_seal_stops_without_relaunch(self):
        driver, original = self.failed_registration()
        root = Path(driver.manifest["artifact_root"]) / "runs" / "sealed" / original["run_id"]
        payload = json.loads((root / "result.json").read_text())
        payload["merge_recommendation"] = "request changes"
        (root / "result.json").write_text(json.dumps(payload))
        count = len(self.calls)
        with self.assertRaisesRegex(StageStopped, "sealed|seal"):
            driver.review()
        self.assertEqual(len(self.calls), count)

    def test_source_mutation_refuses_adoption(self):
        driver, original = self.failed_registration()
        (Path(driver.manifest["worktree_path"]) / "source.py").write_text("value = 999\n")
        count = len(self.calls)
        with self.assertRaisesRegex((ValueError, RuntimeError), "dirty|changed"):
            driver.review()
        self.assertEqual(len(self.calls), count)

    def test_unclassified_validation_failure_does_not_get_accepted(self):
        driver, original = self.failed_registration()
        with database.connect(self.db) as conn:
            dispatch = dict(original["dispatch"])
            dispatch.pop("result_checkpoint", None)
            dispatch.pop("failure_kind", None)
            store.update_run(conn, original["run_id"], dispatch_json=json.dumps(dispatch), error="invalid schema")
        driver.review()
        with database.connect(self.db) as conn:
            saved = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == original["run_id"])
        self.assertNotIn("artifact_adoption", saved["dispatch"])

    def test_nonzero_process_with_complete_valid_result_can_be_adopted(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        driver.request["workers"] = driver.request["workers"][:1]
        launch = driver.launcher

        def failed_exit(**kwargs):
            return {**launch(**kwargs), "returncode": 1}

        driver.launcher = failed_exit
        with self.assertRaisesRegex(RuntimeError, "exited with code"):
            driver.review()
        count = len(self.calls)
        records = driver.review()
        self.assertEqual(len(self.calls), count)
        self.assertEqual(len(records), 1)
        with database.connect(self.db) as conn:
            saved = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == records[0]["run_id"])
        self.assertEqual(saved["status"], "failed")
        self.assertIn("exited with code", saved["error"])

    def test_pending_launch_checkpoint_never_retries_unknown_spawn(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        driver.request["workers"] = driver.request["workers"][:1]
        with database.connect(self.db) as conn:
            run = store.new_run(
                conn,
                driver.round_id,
                "review",
                driver.request["workers"][0]["name"],
                driver.attempt_id,
                driver.manifest["artifact_root"],
                driver.manifest["prompt_sha256"],
            )
            store.update_run(
                conn, run["run_id"], status="starting", dispatch_json=json.dumps({"launch_checkpoint": {}})
            )
        with self.assertRaisesRegex(StageStopped, "unknown"):
            driver.review()
        self.assertEqual(len(self.calls), 0)
