import json
from pathlib import Path
from unittest.mock import patch

from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.workflows.pre_pr_review.coordinator import Coordinator
from tests.support import database as fixture_database
from tests.support.pre_pr_review import PrePRReviewFixture


class LegacyFinalVerificationTests(PrePRReviewFixture):
    def assert_final_rejected(self, mode):
        driver = self.start(["claude"])
        self.with_finding = True
        self.final_review_mode = mode
        if mode in {"blocked", "unknown", "blocking_approval"}:
            with patch.object(driver.stopping, "wait", return_value=False):
                result = driver.run()
            self.assertTrue(result["ok"], result)
            with fixture_database.connect(self.db) as conn:
                self.assertEqual(store.session(conn, task_id=driver.task_id)["status"], "waiting_for_user")
        else:
            with patch.object(driver.stopping, "wait", return_value=False), self.assertRaises(RuntimeError):
                driver.run()
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM github_actions WHERE action_type='open_pr'").fetchone()[0], 0
            )
            if mode not in {"blocked", "unknown", "blocking_approval"}:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 0)
        self.assertLessEqual(self.launches["verify_final"], 3)

    def test_wrong_sha_cannot_release_a_result(self):
        self.assert_final_rejected("wrong_sha")

    def test_blocking_final_approval_cannot_release_a_result(self):
        self.assert_final_rejected("blocking_approval")

    def test_blocked_final_review_cannot_release_a_result(self):
        self.assert_final_rejected("blocked")

    def test_unknown_final_review_cannot_release_a_result(self):
        self.assert_final_rejected("unknown")

    def test_failed_final_review_cannot_release_a_result(self):
        self.assert_final_rejected("failed")

    def test_final_reviewer_restart_adopts_output_without_relaunching_completed_stages(self):
        driver = self.start(["claude"])
        self.with_finding = True
        wait = driver._wait

        def interrupt_final(run, launched, timeout):
            if run["stage"] == "verify_final":
                raise KeyboardInterrupt("coordinator restarted during final review")
            return wait(run, launched, timeout)

        with patch.object(driver, "_wait", side_effect=interrupt_final), self.assertRaises(KeyboardInterrupt):
            driver.run()
        with fixture_database.connect(self.db) as conn:
            final = [run for run in store.runs(conn, driver.round_id) if run["stage"] == "verify_final"]
            self.assertEqual(final[0]["status"], "running")
            self.assertEqual(final[0]["slot_reserved"], 1)
        restarted = Coordinator(
            self.db, driver.task_id, driver.attempt_id, launcher=self.launch_pr_stage, poll_seconds=0.01
        )
        self.assertTrue(restarted.run()["ok"])
        self.assertEqual(self.launches["reviewer-prompt.md"], 1)
        self.assertEqual(self.launches["address"], 1)
        self.assertEqual(self.launches["verify_final"], 1)

    def test_final_verifier_reuses_the_finished_address_worktree(self):
        driver = self.start(["claude"])
        self.with_finding = True
        self.assertTrue(driver.run()["ok"])
        self.assertEqual(self.launches["verify_final"], 1)
        with fixture_database.connect(self.db) as conn:
            address = next(run for run in store.runs(conn, driver.round_id) if run["stage"] == "address_findings")
            self.assertEqual(address["slot_reserved"], 0)
            self.assertEqual(address["status"], "succeeded")
            entry = store.session(conn, task_id=driver.task_id)
            proof = entry["proof"]["final_verification"]
            self.assertEqual(proof["final_sha"], entry["proof"]["head_sha"])
            self.assertEqual(proof["round_id"], driver.round_id)
            self.assertTrue(store.matches_submission(entry, driver.attempt_id, entry["proof"]["fingerprint"]))
            entry["proof"]["final_verification"]["final_sha"] = entry["proof"]["candidate_sha"]
            self.assertFalse(store.matches_submission(entry, driver.attempt_id, entry["proof"]["fingerprint"]))
        self.assertEqual(Path(driver.final_review["worktree_path"]), driver.address_workspace(address["run_id"]))
        self.assertFalse((Path(driver.repo["worktree_root"]) / "pre-pr-final").exists())

    def test_native_address_resume_keeps_original_writer_and_final_verification_tree(self):
        import os

        driver = self.start(["claude"])
        self.with_finding = True
        launch = driver.launcher
        addresses = []
        sid = "01a1f6d9-45c5-7670-8957-9abde29f8db4"

        def interrupted_address(**kwargs):
            result = launch(**kwargs)
            prompt = Path(kwargs["prompt_path"])
            if not prompt.name.startswith("address-"):
                return result
            addresses.append(kwargs)
            if len(addresses) > 1:
                return result
            output, tree = Path(kwargs["log_dir"]), str(kwargs["worktree_path"])
            (output / "dispositions.json").unlink()
            agent = kwargs["worker_agent_config"]["agent"]
            if agent in {"codex", "tcodex"}:
                from datetime import datetime, timezone

                root = self.root / ("." + agent) / "sessions" / datetime.now(timezone.utc).strftime("%Y/%m/%d")
                history = root / f"rollout-2026-11-24T00-00-00-{sid}.jsonl"
                rows = [
                    {"type": "session_meta", "payload": {"id": sid, "source": "exec", "cwd": tree}},
                    {
                        "type": "response_item",
                        "payload": {"type": "message", "role": "user", "content": [{"text": prompt.read_text()}]},
                    },
                ]
                stdout = {"type": "thread.started", "thread_id": sid}
            else:
                import re

                root = self.root / ("." + agent) / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", tree)
                history = root / f"{sid}.jsonl"
                rows = [
                    {
                        "type": "user",
                        "sessionId": sid,
                        "cwd": tree,
                        "message": {"role": "user", "content": prompt.read_text()},
                    }
                ]
                stdout = {"type": "system", "subtype": "init", "session_id": sid}
            root.mkdir(parents=True, exist_ok=True)
            history.write_text("".join(json.dumps(row) + "\n" for row in rows))
            log = output / "stdout.log"
            log.write_text(json.dumps(stdout) + "\n")
            return {**result, "returncode": 1, "stdout_path": str(log)}

        driver.launcher = interrupted_address
        native_home = self.root / ".codex"
        native_home.mkdir(exist_ok=True)
        with (
            patch("pathlib.Path.home", return_value=self.root),
            patch.dict(os.environ, {"CODEX_HOME": str(native_home)}),
        ):
            result = driver.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(addresses), 2)
        with fixture_database.connect(self.db) as conn:
            address_runs = [r for r in store.runs(conn, driver.round_id) if r["stage"] == "address_findings"]
        self.assertIn("resume", addresses[1], [r["dispatch"].get("resume_fallback_reason") for r in address_runs])
        self.assertEqual(addresses[0]["worktree_path"], addresses[1]["worktree_path"])
        self.assertEqual(driver.final_review["worktree_path"], addresses[0]["worktree_path"])
        self.assertEqual(self.launches["implement"], 1)
        self.assertEqual(self.launches["reviewer-prompt.md"], 1)
        self.assertEqual(self.launches["verify_final"], 1)
