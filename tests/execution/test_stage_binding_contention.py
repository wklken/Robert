"""A busy session-binding checkpoint must preserve a running model stage."""

import json
import sqlite3
import subprocess
import sys
from unittest.mock import patch

from robert_agent.adapters.processes import identity
from robert_agent.persistence.rounds import review as store
from tests.support import database
from tests.support.review_coordinator import ReviewCoordinatorFixture


class StageBindingContentionTests(ReviewCoordinatorFixture):
    def running_stage(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
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
        process = subprocess.Popen(
            [sys.executable, "-c", "import sys; print('ready',flush=True); sys.stdin.readline()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(identity.cleanup_new_process, process)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.stdin.close)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        launched = {
            "ok": True,
            **identity.capture_process_identity(process.pid),
            "prompt_path": driver.manifest["prompt_path"],
            "worktree_path": driver.manifest["worktree_path"],
        }
        with database.connect(self.db) as conn:
            store.update_run(conn, run["run_id"], status="running", dispatch_json=json.dumps(launched))
        run["dispatch"] = dict(launched)
        driver.processes[process.pid] = process
        driver.poll_seconds = 0
        return driver, run, process, launched

    def test_busy_binding_retries_without_stopping_worker_or_rewriting_unchanged_binding(self):
        driver, run, process, launched = self.running_stage()
        writer = sqlite3.connect(self.db)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        binding = {"session_id": "binding-arrives-after-launch", "history_path": str(self.root / "history.jsonl")}
        polls = []
        real_poll = process.poll
        clock = iter(range(0, 1000, 2))

        def bind(_run, recorded, _worker, _prompt):
            recorded.update(native_session=binding, native_session_id=binding["session_id"])

        def poll():
            polls.append(True)
            if len(polls) == 1:
                self.assertIsNone(real_poll(), "the model must survive the busy checkpoint")
                with database.connect(self.db) as conn:
                    saved = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == run["run_id"])
                self.assertNotIn("native_session", saved["dispatch"])
                writer.rollback()
            elif len(polls) == 2:
                with database.connect(self.db) as conn:
                    saved = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == run["run_id"])
                self.assertEqual(saved["dispatch"]["native_session"], binding)
                self.assertEqual(update.call_count, 1, "the busy transaction must retry after the lock clears")
            else:
                self.assertEqual(update.call_count, 1, "unchanged binding must not write every poll")
                process.stdin.write("finish\n")
                process.stdin.flush()
                return process.wait(timeout=5)
            return real_poll()

        try:
            with (
                patch.object(driver, "bind_native_session", side_effect=bind),
                patch.object(process, "poll", side_effect=poll),
                patch("robert_agent.execution.stages.time.monotonic", side_effect=lambda: next(clock)),
                patch.object(store, "update_run", wraps=store.update_run) as update,
            ):
                driver._wait(run, launched, 60)
        finally:
            writer.rollback()
        self.assertEqual(process.returncode, 0)
        with database.connect(self.db) as conn:
            saved = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == run["run_id"])
        self.assertEqual(saved["dispatch"]["native_session"], binding)
        self.assertTrue(saved["dispatch"]["process_exited"])
        self.assertIn("result_checkpoint", saved["dispatch"])

    def test_non_busy_binding_error_is_not_suppressed(self):
        driver, run, process, launched = self.running_stage()

        def bind(_run, recorded, _worker, _prompt):
            recorded["native_session_id"] = "discovered-session"

        with (
            patch.object(driver, "bind_native_session", side_effect=bind),
            patch.object(store, "update_run", side_effect=sqlite3.OperationalError("disk I/O error")),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O error"):
                driver._wait(run, launched, 60)
        self.assertIsNone(process.poll())
        with database.connect(self.db) as conn:
            saved = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == run["run_id"])
        self.assertNotIn("process_exited", saved["dispatch"])

    def test_busy_completion_checkpoint_still_blocks_result_and_slot_release(self):
        driver, run, process, launched = self.running_stage()
        process.stdin.write("finish\n")
        process.stdin.flush()
        process.wait(timeout=5)
        with (
            patch.object(driver, "bind_native_session"),
            patch.object(store, "update_run", side_effect=sqlite3.OperationalError("database is locked")),
            patch.object(driver, "release_slot") as release,
            patch.object(driver, "checkpoint_result") as checkpoint,
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                driver._wait(run, launched, 60)
        release.assert_not_called()
        checkpoint.assert_not_called()
