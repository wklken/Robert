"""Stopped stages retain proven exit and source facts without changing owners."""

import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from robert_agent.execution import stages
from robert_agent.persistence.rounds import review as store
from tests.support import database
from tests.support.review_coordinator import ReviewCoordinatorFixture


class StageStoppedTests(ReviewCoordinatorFixture):
    def prepare_driver(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        driver.request["workers"] = driver.request["workers"][:1]
        return driver

    def saved_run(self, driver):
        with database.connect(self.db) as conn:
            return store.runs(conn, driver.round_id)[-1]

    def exited_run(self, driver, *, checkpoint=True):
        worker = driver.request["workers"][0]
        with database.connect(self.db) as conn:
            run = store.new_run(
                conn,
                driver.round_id,
                "review",
                worker["name"],
                driver.attempt_id,
                driver.manifest["artifact_root"],
                driver.manifest["prompt_sha256"],
                worker["default_model"],
            )
        launch = driver.source_checkpoint(run)
        source = Path(driver.manifest["worktree_path"]) / "source.py"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('value = 7\\n')",
                str(source),
            ],
            start_new_session=True,
        )
        self.assertEqual(proc.wait(timeout=5), 0)
        recorded = {"ok": True, "pid": proc.pid, "process_group_id": proc.pid, "worktree_path": launch["worktree_path"]}
        if checkpoint:
            recorded["launch_checkpoint"] = launch
        with database.connect(self.db) as conn:
            store.update_run(conn, run["run_id"], status="running", slot_reserved=1, dispatch_json=json.dumps(recorded))
        return self.saved_run(driver), launch

    def test_cleanup_dead_group_marks_interrupted_and_pins_writer_progress(self):
        driver = self.prepare_driver()
        original, launch = self.exited_run(driver)
        with store.connect(self.db) as conn:
            self.assertEqual(stages.terminate_children(conn, driver.task_id, store=store), "terminated")
        saved = self.saved_run(driver)
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual(saved["error"], "coordinator stopped")
        self.assertFalse(saved["slot_reserved"])
        self.assertTrue(saved["dispatch"]["process_exited"])
        self.assertTrue(saved["dispatch"]["group_exit_confirmed"])
        self.assertEqual(saved["dispatch"]["launch_checkpoint"], launch)
        self.assertEqual(saved["dispatch"]["result_checkpoint"], driver.source_checkpoint(saved))
        self.assertNotEqual(saved["dispatch"]["result_checkpoint"], launch)
        self.assertEqual(saved["owner_attempt_id"], original["owner_attempt_id"])
        with database.connect(self.db) as conn:
            usage = json.loads(
                conn.execute("SELECT usage_json FROM model_usage WHERE call_id=?", (saved["run_id"],)).fetchone()[0]
            )
        self.assertIn("process_exit_observed", usage["segments"][saved["run_id"]]["events"])

    def test_unknown_group_keeps_running_and_reservation(self):
        driver = self.prepare_driver()
        original, _ = self.exited_run(driver)
        with (
            patch.object(stages, "_group_finished", return_value=False),
            patch.object(stages, "_wait_for_group_exit", return_value=False),
        ):
            with store.connect(self.db) as conn:
                self.assertEqual(stages.terminate_children(conn, driver.task_id, store=store), "attention")
        self.assertEqual(self.saved_run(driver), original)

    def test_cleanup_refuses_changed_run_even_without_source_checkpoint(self):
        driver = self.prepare_driver()
        original, _ = self.exited_run(driver, checkpoint=False)
        finished = stages._group_finished

        def changed(recorded):
            with database.connect(self.db) as conn:
                store.update_run(conn, original["run_id"], error="new owner observation")
            return finished(recorded)

        with patch.object(stages, "_group_finished", side_effect=changed):
            with store.connect(self.db) as conn:
                self.assertEqual(stages.terminate_children(conn, driver.task_id, store=store), "attention")
        saved = self.saved_run(driver)
        self.assertEqual(saved["status"], "running")
        self.assertEqual(saved["error"], "new owner observation")
        self.assertTrue(saved["slot_reserved"])
        self.assertEqual(saved["dispatch"], original["dispatch"])

    def test_stopped_validation_records_interruption_and_adopts_without_model(self):
        driver = self.prepare_driver()
        with patch.object(driver, "validate_output", side_effect=stages.StageStopped("validation needs attention")):
            with self.assertRaisesRegex(stages.StageStopped, "validation needs attention"):
                driver.review()
        saved = self.saved_run(driver)
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual(saved["error"], "validation needs attention")
        self.assertTrue(saved["dispatch"]["group_exit_confirmed"])
        self.assertEqual(saved["dispatch"]["result_checkpoint"], driver.source_checkpoint(saved))
        calls = len(self.calls)
        recovered = self.coordinator()
        recovered.request["workers"] = recovered.request["workers"][:1]
        self.assertEqual(recovered.review()[0]["run_id"], saved["run_id"])
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.saved_run(driver)["status"], "interrupted")

    def test_stopped_validation_cannot_overwrite_rebound_owner(self):
        driver = self.prepare_driver()

        def stop(*args):
            with database.connect(self.db) as conn:
                conn.execute(
                    "UPDATE execution_subjects SET binding_status='terminal' WHERE subject_id=?", (driver.task_id,)
                )
            raise stages.StageStopped("owner changed")

        with patch.object(driver, "validate_output", side_effect=stop):
            with self.assertRaisesRegex(stages.StageStopped, "owner changed"):
                driver.review()
        saved = self.saved_run(driver)
        self.assertEqual(saved["status"], "running")
        self.assertIsNone(saved["error"])

    def test_stopped_unknown_group_retains_slot_and_has_no_terminal_usage(self):
        driver = self.prepare_driver()
        with (
            patch.object(stages, "_group_finished", return_value=False),
            patch.object(stages, "_wait_for_group_exit", return_value=False),
        ):
            with self.assertRaisesRegex(stages.StageStopped, "process group"):
                driver.review()
        saved = self.saved_run(driver)
        self.assertEqual(saved["status"], "running")
        self.assertTrue(saved["slot_reserved"])
        self.assertNotIn("group_exit_confirmed", saved["dispatch"])
        with database.connect(self.db) as conn:
            usage = json.loads(
                conn.execute("SELECT usage_json FROM model_usage WHERE call_id=?", (saved["run_id"],)).fetchone()[0]
            )
        self.assertNotIn("process_exit_observed", usage["segments"][saved["run_id"]]["events"])

    def test_cleanup_preserves_failed_invocation_history(self):
        driver = self.prepare_driver()
        original, _ = self.exited_run(driver)
        recorded = {**original["dispatch"], "returncode": 9, "failure_kind": "execution_complete"}
        with database.connect(self.db) as conn:
            store.update_run(
                conn, original["run_id"], status="failed", error="exit code 9", dispatch_json=json.dumps(recorded)
            )
        with store.connect(self.db) as conn:
            self.assertEqual(stages.terminate_children(conn, driver.task_id, store=store), "terminated")
        saved = self.saved_run(driver)
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["error"], "exit code 9")
        self.assertEqual(saved["dispatch"]["returncode"], 9)
        self.assertEqual(saved["dispatch"]["failure_kind"], "execution_complete")
        self.assertEqual(saved["dispatch"]["launch_checkpoint"], recorded["launch_checkpoint"])

    def test_source_observation_has_no_writer_and_rechecks_changed_run(self):
        driver = self.prepare_driver()
        original, _ = self.exited_run(driver)
        observe = stages.stage_evidence.observe_source

        def changed(*args):
            with database.connect(self.db) as conn:
                conn.execute("BEGIN IMMEDIATE")
                store.update_run(conn, original["run_id"], error="concurrent source observation")
            return observe(*args)

        with patch.object(stages.stage_evidence, "observe_source", side_effect=changed):
            driver.checkpoint_stopped_run(original, stages.StageStopped("stopped"))
        saved = self.saved_run(driver)
        self.assertEqual(saved["status"], "running")
        self.assertTrue(saved["slot_reserved"])
        self.assertEqual(saved["error"], "concurrent source observation")
        self.assertEqual(saved["dispatch"], original["dispatch"])

    def test_reconcile_stopped_finished_group_is_interrupted(self):
        driver = self.prepare_driver()
        original, _ = self.exited_run(driver)
        with patch.object(driver, "_wait", side_effect=stages.StageStopped("reconcile needs attention")):
            with self.assertRaisesRegex(stages.StageStopped, "reconcile needs attention"):
                driver.reconcile_running()
        saved = self.saved_run(driver)
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual(saved["error"], "reconcile needs attention")
        self.assertEqual(saved["dispatch"]["result_checkpoint"], driver.source_checkpoint(saved))
        self.assertEqual(saved["owner_attempt_id"], original["owner_attempt_id"])

    def test_cleanup_preserves_existing_result_pin_when_source_drifts(self):
        driver = self.prepare_driver()
        original, _ = self.exited_run(driver)
        checkpoint = driver.source_checkpoint(original)
        with database.connect(self.db) as conn:
            recorded = {**original["dispatch"], "result_checkpoint": checkpoint}
            store.update_run(conn, original["run_id"], dispatch_json=json.dumps(recorded))
        (Path(driver.manifest["worktree_path"]) / "source.py").write_text("value = 8\n")
        with store.connect(self.db) as conn:
            self.assertEqual(stages.terminate_children(conn, driver.task_id, store=store), "terminated")
        saved = self.saved_run(driver)
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual(saved["dispatch"]["result_checkpoint"], checkpoint)
        self.assertEqual(saved["dispatch"]["resume_fallback_reason"], "stopped_source_unavailable")

    def test_terminal_cancel_caller_writer_never_observes_git(self):
        driver = self.prepare_driver()
        original, _ = self.exited_run(driver)
        with patch.object(stages.stage_evidence, "observe_source", side_effect=AssertionError("Git under writer")):
            with database.connect(self.db) as conn:
                conn.execute("BEGIN IMMEDIATE")
                self.assertEqual(stages.terminate_children(conn, driver.task_id, store=store), "terminated")
        saved = self.saved_run(driver)
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual(saved["dispatch"]["launch_checkpoint"], original["dispatch"]["launch_checkpoint"])
        self.assertTrue(saved["dispatch"]["group_exit_confirmed"])

    def test_cleanup_refuses_binding_change_after_exit_observation(self):
        driver = self.prepare_driver()
        original, _ = self.exited_run(driver, checkpoint=False)
        finished = stages._group_finished

        def rebound(recorded):
            with database.connect(self.db) as conn:
                conn.execute(
                    "UPDATE execution_subjects SET binding_status='terminal' WHERE subject_id=?", (driver.task_id,)
                )
            return finished(recorded)

        with patch.object(stages, "_group_finished", side_effect=rebound):
            with store.connect(self.db) as conn:
                self.assertEqual(stages.terminate_children(conn, driver.task_id, store=store), "attention")
        self.assertEqual(self.saved_run(driver), original)
