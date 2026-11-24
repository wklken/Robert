"""Stage launch/exit/adoption callers preserve a single durable model receipt."""

import json
from pathlib import Path
from unittest.mock import patch

from robert_agent.persistence.queries import model_usage
from robert_agent.persistence.rounds import review
from tests.support import database
from tests.support.review_coordinator import ReviewCoordinatorFixture


class ModelUsageIntegrationTests(ReviewCoordinatorFixture):
    def test_real_stage_hooks_pending_before_launch_exit_then_adoption_no_extra_call(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        driver.request["workers"] = driver.request["workers"][:1]
        driver.request["workers"][0]["agent"] = "tclaude"
        original_launcher = driver.launcher

        def launch(**kwargs):
            call_id = kwargs["attempt_id"]
            with database.connect(self.db) as conn:
                pending = json.loads(model_usage.get(conn, "stage", call_id)["usage_json"])
            self.assertEqual(pending["measurement_status"], "pending")
            segment = pending["segments"][call_id]
            self.assertFalse(segment["launched"])
            self.assertIn("launch_requested", segment["events"])
            self.assertNotIn("started", segment["events"])
            result = original_launcher(**kwargs)
            path = Path(kwargs["log_dir"]) / "stdout.log"
            path.write_text(
                json.dumps(
                    {"type": "result", "duration_ms": 600000, "usage": {"input_tokens": 100, "output_tokens": 20}}
                )
                + "\n"
            )
            return {**result, "stdout_path": str(path), "worker_agent": "tclaude"}

        driver.launcher = launch
        with patch.object(driver, "register", side_effect=ValueError("registration unavailable")):
            with self.assertRaisesRegex(RuntimeError, "registration unavailable"):
                driver.review()
        with database.connect(self.db) as conn:
            run = next(run for run in review.runs(conn, driver.round_id) if run["stage"] == "review")
            complete = json.loads(model_usage.get(conn, "stage", run["run_id"])["usage_json"])
        self.assertEqual(complete["measurement_status"], "complete")
        self.assertEqual(complete["source"], "claude_stream_json")
        segment = complete["segments"][run["run_id"]]
        self.assertTrue(segment["launched"])
        self.assertIn("started", segment["events"])
        self.assertIn("process_exit_observed", segment["events"])
        launches = len(self.calls)
        driver.review()
        driver.review()
        self.assertEqual(len(self.calls), launches)
        with database.connect(self.db) as conn:
            adopted = json.loads(model_usage.get(conn, "stage", run["run_id"])["usage_json"])
        self.assertEqual(adopted["usage"], complete["usage"])
        self.assertEqual(len(adopted["segments"]), 1)
        self.assertIn("artifact_adopted", adopted["segments"][run["run_id"]]["events"])
