"""Resume only a stopped incomplete stage while preserving successful siblings."""

from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import patch

from robert_agent.execution.stages import StageStopped
from robert_agent.persistence.rounds import review as store
from tests.support import database
from tests.support.review_coordinator import ReviewCoordinatorFixture


class StageNativeResumeTests(ReviewCoordinatorFixture):
    def prepare_interrupted(self):
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
        checkpoint = driver.source_checkpoint(run)
        root = self.root / ".codex" / "sessions" / datetime.now(timezone.utc).strftime("%Y/%m/%d")
        root.mkdir(parents=True)
        sid = "01a1f6d9-45c5-7670-8957-9abde29f8db4"
        history = root / f"rollout-2026-11-24T00-00-00-{sid}.jsonl"
        prompt = Path(driver.manifest["prompt_path"])
        rows = [
            {"type": "session_meta", "payload": {"id": sid, "source": "exec", "cwd": driver.manifest["worktree_path"]}},
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": [{"text": prompt.read_text()}]},
            },
        ]
        history.write_text("".join(json.dumps(row) + "\n" for row in rows))
        stdout = Path(run["output_dir"]) / "stdout.log"
        stdout.write_text(json.dumps({"type": "thread.started", "thread_id": sid}) + "\n")
        dispatch = {
            "ok": True,
            "pid": 1073741824,
            "process_exited": True,
            "returncode": 1,
            "worker_agent": "codex",
            "prompt_path": str(prompt),
            "stdout_path": str(stdout),
            "worktree_path": driver.manifest["worktree_path"],
            "launch_checkpoint": checkpoint,
            "result_checkpoint": checkpoint,
            "failure_kind": "interrupted",
        }
        with database.connect(self.db) as conn:
            store.update_run(
                conn,
                run["run_id"],
                status="interrupted",
                error="coordinator stopped",
                dispatch_json=json.dumps(dispatch),
            )
        return driver, run, history

    def test_resume_one_incomplete_reviewer_then_cache_without_relaunch(self):
        with patch("pathlib.Path.home", return_value=self.root):
            driver, original, history = self.prepare_interrupted()
            launch = driver.launcher
            calls = []

            def capture(**kwargs):
                calls.append(kwargs)
                return launch(**kwargs)

            driver.launcher = capture
            records = driver.review()
            resumes = [kwargs for kwargs in calls if kwargs.get("resume")]
            self.assertEqual(len(resumes), 1)
            self.assertEqual(resumes[0]["resume"]["parent_run_id"], original["run_id"])
            self.assertEqual(resumes[0]["resume"]["history_path"], str(history))
            self.assertEqual(resumes[0]["prompt_path"], driver.manifest["prompt_path"])
            with database.connect(self.db) as conn:
                runs = store.runs(conn, driver.round_id)
                self.assertEqual(next(r for r in runs if r["run_id"] == original["run_id"])["status"], "interrupted")
                resumed = next(r for r in runs if r["dispatch"].get("native_resume"))
                self.assertEqual(resumed["dispatch"]["parent_run_id"], original["run_id"])
            count = len(calls)
            driver.review()
            self.assertEqual(len(calls), count)
            self.assertEqual(len(records), 2)

    def test_missing_history_falls_back_only_affected_stage_with_reason(self):
        with patch("pathlib.Path.home", return_value=self.root):
            driver, original, history = self.prepare_interrupted()
            history.unlink()
            records = driver.review()
            with database.connect(self.db) as conn:
                run = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == records[0]["run_id"])
            self.assertNotIn("native_resume", run["dispatch"])
            self.assertEqual(run["dispatch"]["resume_fallback_reason"], "native_history_unavailable")

    def test_resume_keeps_original_native_home_when_controller_environment_changes(self):
        with patch("pathlib.Path.home", return_value=self.root):
            driver, original, _ = self.prepare_interrupted()
            changed_home = self.root / "new-controller-home"
            changed_home.mkdir()
            with patch("robert_agent.execution.stages.native_home_from_environment", return_value=str(changed_home)):
                driver.review()
            with database.connect(self.db) as conn:
                resumed = next(r for r in store.runs(conn, driver.round_id) if r["dispatch"].get("native_resume"))
            self.assertEqual(resumed["dispatch"]["parent_run_id"], original["run_id"])
            self.assertEqual(resumed["dispatch"]["native_home"], str(self.root / ".codex"))
            self.assertEqual(resumed["dispatch"]["native_home"], resumed["dispatch"]["native_resume"]["native_home"])

    def test_live_original_group_prevents_resume_and_fresh(self):
        with patch("pathlib.Path.home", return_value=self.root):
            driver, original, history = self.prepare_interrupted()
            with patch("robert_agent.execution.stages._group_finished", return_value=False):
                with self.assertRaisesRegex(StageStopped, "identity|group"):
                    driver.review()
            self.assertEqual(len(self.calls), 0)

    def test_early_session_binding_is_upgraded_when_history_arrives(self):
        with patch("pathlib.Path.home", return_value=self.root):
            driver, original, history = self.prepare_interrupted()
            from robert_agent.adapters.processes.session_resume import resolve_native_session

            with database.connect(self.db) as conn:
                run = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == original["run_id"])
                recorded = run["dispatch"]
                binding = resolve_native_session(
                    data_root=driver.data_dir,
                    agent="codex",
                    task_id=driver.task_id,
                    run_id=run["run_id"],
                    prompt_path=driver.manifest["prompt_path"],
                    prompt_sha256=run["prompt_sha256"],
                    cwd=recorded["worktree_path"],
                    stdout_path=recorded["stdout_path"],
                    started_at=run["created_at"],
                )
                self.assertIsNotNone(binding)
                binding.update(history_root=None, history_path=None, history_identity=None)
                recorded["native_session"] = binding
                store.update_run(conn, run["run_id"], dispatch_json=json.dumps(recorded))
            driver.review()
            with database.connect(self.db) as conn:
                resumed = next(
                    (r for r in store.runs(conn, driver.round_id) if r["dispatch"].get("native_resume")), None
                )
            self.assertIsNotNone(resumed)
            self.assertEqual(resumed["dispatch"]["native_resume"]["history_path"], str(history))

    def test_dirty_writer_bytes_cannot_change_behind_saved_status(self):
        with patch("pathlib.Path.home", return_value=self.root):
            driver, original, _ = self.prepare_interrupted()
            source = Path(driver.manifest["worktree_path"]) / "source.py"
            source.write_text("value = 7\n")
            with database.connect(self.db) as conn:
                run = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == original["run_id"])
            checkpoint = driver.source_checkpoint(run)
            run["dispatch"]["result_checkpoint"] = checkpoint
            source.write_text("value = 8\n")
            self.assertNotEqual(driver.source_checkpoint(run), checkpoint)

    def test_ignored_runtime_configuration_is_part_of_source_pin(self):
        with patch("pathlib.Path.home", return_value=self.root):
            driver, original, _ = self.prepare_interrupted()
            tree = Path(driver.manifest["worktree_path"])
            (tree / ".gitignore").write_text("runtime-config.json\n")
            config = tree / "runtime-config.json"
            config.write_text('{"enabled": true}\n')
            with database.connect(self.db) as conn:
                run = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == original["run_id"])
            checkpoint = driver.source_checkpoint(run)
            config.write_text('{"enabled": false}\n')
            self.assertNotEqual(driver.source_checkpoint(run), checkpoint)

    def test_stopped_writer_progress_is_pinned_before_interruption_recovery(self):
        with patch("pathlib.Path.home", return_value=self.root):
            driver, original, _ = self.prepare_interrupted()
            tree = Path(driver.manifest["worktree_path"])
            (tree / "source.py").write_text("value = 7\n")
            from robert_agent.execution.stages import terminate_children

            with database.connect(self.db) as conn:
                run = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == original["run_id"])
                recorded = run["dispatch"]
                recorded.pop("result_checkpoint")
                store.update_run(conn, run["run_id"], status="running", dispatch_json=json.dumps(recorded))
            with database.connect(self.db) as conn:
                self.assertNotEqual(terminate_children(conn, driver.task_id, store=store), "attention")
            with database.connect(self.db) as conn:
                stopped = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == original["run_id"])
            self.assertEqual(stopped["dispatch"]["result_checkpoint"], driver.source_checkpoint(stopped))
            self.assertNotEqual(stopped["dispatch"]["result_checkpoint"], stopped["dispatch"]["launch_checkpoint"])
