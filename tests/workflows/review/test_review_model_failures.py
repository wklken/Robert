import json
import os
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import yaml

from robert_agent.persistence.rounds import review as review_store
from robert_agent.workflows.review import coordinator as review_coordinator
from tests.support import database as fixture_database
from tests.support.review_coordinator import ReviewCoordinatorFixture


class ReviewModelFailureTests(ReviewCoordinatorFixture):
    def test_failed_model_retry_reuses_other_result_and_exact_prompt(self):
        self.cycle([self.event()])
        self.fail_worker = "claude"
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.coordinator().run()
        self.fail_worker = None
        self.coordinator().run()
        counts = Counter((call[0], call[1]) for call in self.calls)
        self.assertEqual(counts[("prepare", "codex")], 0)
        self.assertEqual(counts[("review", "codex")], 1)
        self.assertEqual(counts[("review", "claude")], 2)
        self.assertEqual(counts[("synthesis", "codex")], 1)
        prompts = [call[2] for call in self.calls if call[0] == "review"]
        self.assertEqual(len(set(prompts)), 1)

    def test_timeout_fallback_is_per_reviewer_and_survives_file_recovery(self):
        config = yaml.safe_load(self.config_path.read_text())
        config["workers"]["claude"]["model"] = "primary-model"
        config["workers"]["claude"]["fallback_model"] = "fallback-model"
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([self.event()])
        original_wait = review_coordinator.Coordinator._wait

        def timeout(driver, run, launched, seconds):
            if run["stage"] == "review" and run["worker_id"] == "claude":
                raise RuntimeError("review worker timed out")
            return original_wait(driver, run, launched, seconds)

        with patch.object(review_coordinator.Coordinator, "_wait", timeout):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    self.coordinator().run()
        driver = self.coordinator()
        original_register = driver.register

        def crash(run, artifact):
            if run["stage"] == "review" and run["worker_id"] == "claude":
                raise KeyboardInterrupt("fallback file before registration")
            return original_register(run, artifact)

        with patch.object(driver, "register", side_effect=crash), self.assertRaises(KeyboardInterrupt):
            driver.run()
        report = self.coordinator().run()
        reviews = [call for call in self.calls if call[0] == "review"]
        self.assertEqual(
            [call[5] for call in reviews if call[1] == "claude"], ["primary-model", "primary-model", "fallback-model"]
        )
        self.assertEqual(sum(call[1] == "codex" for call in reviews), 1)
        self.assertEqual(len({call[2] for call in reviews}), 1)
        self.assertIn("fallback-model", report.read_text())
        with fixture_database.connect(self.db) as conn:
            selected = [
                run
                for run in review_store.runs(conn, driver.round_id)
                if run["stage"] == "review" and run["status"] == "succeeded"
            ]
            fallback = next(run for run in selected if run["worker_id"] == "claude")
            self.assertEqual(fallback["model"], "fallback-model")
            self.assertEqual(fallback["artifact"]["model"], "fallback-model")
            self.assertIn("fallback-model", json.dumps(review_store.board_status(conn, driver.task_id)))

    def test_rate_limit_automatically_switches_only_affected_reviewer(self):
        config = yaml.safe_load(self.config_path.read_text())
        config["workers"]["claude"].update(model="primary-model", fallback_model="fallback-model")
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([self.event()])
        driver = self.coordinator()
        original = driver.launcher

        def limited(**kwargs):
            if kwargs["worker_agent_config"]["name"] == "claude" and kwargs["model"] == "primary-model":
                output = Path(kwargs["log_dir"])
                log = output / "stdout.log"
                log.write_text(
                    json.dumps({"type": "turn.failed", "error": {"message": "429 Too Many Requests"}}) + "\n"
                )
                self.calls.append(
                    (
                        "review",
                        "claude",
                        Path(kwargs["prompt_path"]).read_bytes(),
                        kwargs["worktree_path"],
                        str(output),
                        kwargs["model"],
                    )
                )
                return {
                    "ok": False,
                    "pid": 1073741824,
                    "process_exited": True,
                    "returncode": 1,
                    "stdout_path": str(log),
                    "safe_error": "worker exited with code 1",
                }
            return original(**kwargs)

        driver.launcher = limited
        report = driver.run()
        reviews = [call for call in self.calls if call[0] == "review"]
        self.assertEqual([call[5] for call in reviews if call[1] == "claude"], ["primary-model", "fallback-model"])
        self.assertEqual(sum(call[1] == "codex" for call in reviews), 1)
        self.assertEqual(len({call[2] for call in reviews}), 1)
        self.assertIn("fallback-model", report.read_text())

    def test_rate_limited_fallback_stops_and_survives_coordinator_restart(self):
        config = yaml.safe_load(self.config_path.read_text())
        config["workers"]["claude"].update(model="primary-model", fallback_model="fallback-model")
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([self.event()])
        driver = self.coordinator()
        original = driver.launcher
        limited_models = []

        def limited(**kwargs):
            if kwargs["worker_agent_config"]["name"] == "claude":
                limited_models.append(kwargs["model"])
                log = Path(kwargs["log_dir"]) / "stderr.log"
                log.write_text("HTTP 429 Too Many Requests")
                return {
                    "ok": False,
                    "pid": 1073741824,
                    "process_exited": True,
                    "returncode": 1,
                    "stderr_path": str(log),
                    "safe_error": "worker exited with code 1",
                }
            return original(**kwargs)

        driver.launcher = limited
        with self.assertRaisesRegex(RuntimeError, "rate_limited"):
            driver.run()
        restarted = self.coordinator()
        restarted.launcher = limited
        with self.assertRaisesRegex(RuntimeError, "rate_limited"):
            restarted.run()
        self.assertEqual(limited_models, ["primary-model", "fallback-model"])

    def test_rate_limit_switch_after_running_worker_exits(self):
        config = yaml.safe_load(self.config_path.read_text())
        config["workers"]["claude"].update(model="primary-model", fallback_model="fallback-model")
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([self.event()])
        driver = self.coordinator()
        original = driver.launcher
        models = []

        def limited(**kwargs):
            if kwargs["worker_agent_config"]["name"] == "claude":
                models.append(kwargs["model"])
                if kwargs["model"] == "primary-model":
                    log = Path(kwargs["log_dir"]) / "stdout.log"
                    log.write_text(json.dumps({"type": "turn.failed", "error": {"message": "429 Too Many Requests"}}))
                    return {
                        "ok": True,
                        "pid": 1073741824,
                        "process_exited": True,
                        "returncode": 1,
                        "stdout_path": str(log),
                    }
            return original(**kwargs)

        driver.launcher = limited
        self.assertTrue(driver.run().is_file())
        self.assertEqual(models, ["primary-model", "fallback-model"])

    def test_rate_limit_without_distinct_fallback_does_not_relaunch_stage(self):
        for fallback in (None, "primary-model"):
            with self.subTest(fallback=fallback):
                fixture = ReviewCoordinatorFixture()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                config = yaml.safe_load(fixture.config_path.read_text())
                config["workers"]["claude"].update(model="primary-model", fallback_model=fallback)
                if fallback is None:
                    config["workers"]["claude"].pop("fallback_model")
                fixture.config_path.write_text(yaml.safe_dump(config))
                fixture.cycle([fixture.event()])
                driver = fixture.coordinator()
                original = driver.launcher
                calls = []

                def limited(**kwargs):
                    if kwargs["worker_agent_config"]["name"] == "claude":
                        calls.append(kwargs["model"])
                        log = Path(kwargs["log_dir"]) / "stdout.log"
                        log.write_text(
                            json.dumps({"type": "turn.failed", "error": {"message": "429 Too Many Requests"}})
                        )
                        return {
                            "ok": False,
                            "pid": 1073741824,
                            "process_exited": True,
                            "returncode": 1,
                            "stdout_path": str(log),
                            "safe_error": "worker exited with code 1",
                        }
                    return original(**kwargs)

                driver.launcher = limited
                with self.assertRaisesRegex(RuntimeError, "rate_limited"):
                    driver.run()
                self.assertEqual(calls, ["primary-model"])

    def test_other_reviewers_timeouts_do_not_advance_fallback(self):
        config = yaml.safe_load(self.config_path.read_text())
        for worker in config["workers"].values():
            worker.update(model="primary-model", fallback_model="fallback-model")
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([self.event()])
        original_wait = review_coordinator.Coordinator._wait

        def timeout(driver, run, launched, seconds):
            if run["stage"] == "review":
                raise RuntimeError("review worker timed out")
            return original_wait(driver, run, launched, seconds)

        with patch.object(review_coordinator.Coordinator, "_wait", timeout):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                self.coordinator().run()
        self.coordinator().run()
        self.assertEqual({call[5] for call in self.calls if call[0] == "review"}, {"primary-model"})

    def test_cancel_fences_late_file_and_publication(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        original = driver.launcher

        def cancel(**kwargs):
            result = original(**kwargs)
            with fixture_database.connect(self.db) as conn:
                conn.execute("UPDATE tasks SET lifecycle='canceled' WHERE task_id=?", (driver.task_id,))
            return result

        driver.launcher = cancel
        with self.assertRaises(review_coordinator.StageStopped):
            driver.run()
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0], 0)
            self.assertEqual(review_store.session(conn, task_id=driver.task_id)["status"], "canceled")

    @unittest.skipUnless(hasattr(os, "killpg"), "requires process groups")
    def test_exited_model_with_live_child_cannot_register_result(self):
        self.check_lingering_child("success")

    @unittest.skipUnless(hasattr(os, "killpg"), "requires process groups")
    def test_failed_startup_cleans_descendants_before_releasing_capacity(self):
        self.check_lingering_child("failure")

    @unittest.skipUnless(hasattr(os, "killpg"), "requires process groups")
    def test_timeout_with_child_ignoring_termination_requires_attention(self):
        self.check_lingering_child("timeout")
