import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from robert_agent.adapters.git.review_revision import git
from robert_agent.delivery import queries
from robert_agent.delivery.application import publish, submission
from robert_agent.execution import capacity, rounds
from robert_agent.persistence.rounds import pool
from robert_agent.work.application import commands
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.workflows.pre_pr_review.coordinator import Coordinator
from robert_agent.workflows.review import workspace as review_workspace
from tests.support import database as fixture_database
from tests.support.repository_cycle import _verification_evidence
from tests.support.pre_pr_review import PrePRReviewFixture


class PrePublicationReviewTests(PrePRReviewFixture):
    def test_report_cookie_example_can_pass_original_submission_audit(self):
        from robert_agent.workflows.pre_pr_review import coordinator as module

        driver = self.start(["claude"])
        example = "响应头 Set-Cookie: session=<redacted>; Path=/，仍需校验会话处理。"
        render = module.files.render_individual
        with patch.object(module.files, "render_individual", side_effect=lambda *args: render(*args) + "\n" + example):
            result = driver.run()
        self.assertTrue(result["ok"], result)
        with fixture_database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
            self.assertIn(example, Path(entry["report_path"]).read_text())
            (metadata,) = conn.execute(
                "SELECT metadata_json FROM github_actions WHERE action_type='open_pr'"
            ).fetchone()
            body = json.loads(metadata)["body"]
            self.assertIn("仍需校验会话处理", body)
            self.assertIn("Set-Cookie", body)

    def test_invalid_handoff_repairs_only_the_artifact_and_reuses_implementation(self):
        driver = self.start(["claude"])
        launch = driver.launcher
        repairs = []

        def malformed_then_correct(**kwargs):
            prompt = Path(kwargs["prompt_path"])
            output = Path(kwargs["log_dir"])
            if prompt.name.startswith("repair-handoff-"):
                repairs.append(prompt)
                self.fail("deterministic handoff correction must not launch a model")
            result = launch(**kwargs)
            if "ROBERT_PRE_PR_CANDIDATE_DIR" in kwargs["trusted_environment"]:
                path = output / "candidate.json"
                payload = json.loads(path.read_text())
                payload["unexpected_program_field"] = True
                path.write_text(json.dumps(payload))
            return result

        driver.launcher = malformed_then_correct
        result = driver.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches["implement"], 1)
        self.assertEqual(len(repairs), 0)
        corrected = list((driver.root / driver.round_id).glob("corrected-handoffs/*/*/candidate.json"))
        self.assertEqual(len(corrected), 1)
        with fixture_database.connect(self.db) as conn:
            runs = store.runs(conn, driver.round_id)
            original = next(run for run in runs if run["stage"] == "implement")
            pin = original["dispatch"]["handoff_repair_checkpoint"]
            original_path = Path(pin["handoff_path"])
            self.assertEqual(store.digest(original_path), pin["handoff_sha256"])
            expected = json.loads(original_path.read_text())
            self.assertTrue(expected.pop("unexpected_program_field"))
            self.assertEqual(json.loads(corrected[0].read_text()), expected)
            self.assertFalse(any(run["stage"] == "repair_handoff" for run in runs))

    def test_one_targeted_repair_after_final_blocker_then_delivers(self):
        driver = self.start(["claude"])
        self.with_finding = True
        launch = driver.launcher

        def blocker_then_approved(**kwargs):
            result = launch(**kwargs)
            prompt = Path(kwargs["prompt_path"])
            output = Path(kwargs["log_dir"])
            if prompt.name.startswith("verify-final-") and self.launches["verify_final"] == 1:
                payload = json.loads((output / "final-verification.json").read_text())
                payload["recommendation"] = "blocked"
                payload["blocking_findings"] = ["source.py still requires the scoped guard"]
                (output / "final-verification.json").write_text(json.dumps(payload))
            return result

        driver.launcher = blocker_then_approved
        result = driver.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches["implement"], 1)
        self.assertEqual(self.launches["address"], 2)
        self.assertEqual(self.launches["verify_final"], 2)
        with fixture_database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
            self.assertEqual(entry["request"]["final_repair_count"], 1)
            self.assertTrue(all(run["status"] != "running" for run in store.runs(conn, driver.round_id)))

    def test_original_request_is_pinned_for_review_address_and_final_verification(self):
        driver = self.start(["claude"])
        original = Path(driver.request["prompt_path"])
        original.write_text(original.read_text() + "\nOriginal acceptance: keep original error observable.\n")
        driver.request["prompt_sha256"] = store.digest(original)
        self.with_finding = True
        seen = {}
        launch = driver.launcher

        def check_context(**kwargs):
            prompt = Path(kwargs["prompt_path"])
            if "ROBERT_PRE_PR_CANDIDATE_DIR" not in kwargs["trusted_environment"]:
                text = prompt.read_text()
                self.assertIn("original-request", text)
                pinned = driver.manifest["original_request"]
                self.assertIn(pinned["path"], text)
                self.assertEqual(store.digest(pinned["path"]), pinned["sha256"])
                self.assertIn("keep original error observable", Path(pinned["path"]).read_text())
                seen[prompt.name.split("-")[0]] = True
            return launch(**kwargs)

        driver.launcher = check_context
        result = driver.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(set(seen), {"reviewer", "address", "verify"})

    def test_unrelated_base_advance_preserves_completed_reviews_and_repairs(self):
        driver = self.start(["claude"])
        self.with_finding = True
        launch = driver.launcher
        changed = []

        def advance_base(**kwargs):
            result = launch(**kwargs)
            if Path(kwargs["prompt_path"]).name.startswith("verify-final-") and not changed:
                root = self.git_fixture.upstream
                git(root, "checkout", "main")
                (root / "docs").mkdir(exist_ok=True)
                (root / "docs" / "unrelated.md").write_text("unrelated upstream documentation\n")
                git(root, "add", "docs/unrelated.md")
                git(root, "commit", "-m", "advance base independently")
                changed.append(git(root, "rev-parse", "HEAD"))
            return result

        driver.launcher = advance_base
        result = driver.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches["reviewer-prompt.md"], 1)
        self.assertEqual(self.launches["address"], 1)
        self.assertEqual(self.launches["verify_final"], 1)
        with fixture_database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
            self.assertEqual(entry["proof"]["base_sha"], changed[0])
            self.assertEqual(entry["proof"]["base_refresh"]["reviewed_base_sha"], driver.manifest["base_sha"])

    def test_related_base_advance_re_reviews_and_preserves_repaired_commit(self):
        driver = self.start(["claude"])
        self.with_finding = True
        launch = driver.launcher
        changed = []

        def advance_related_base(**kwargs):
            result = launch(**kwargs)
            if Path(kwargs["prompt_path"]).name.startswith("verify-final-") and not changed:
                root = self.git_fixture.upstream
                git(root, "checkout", "main")
                (root / "consumer.py").write_text("from source import value\nbase_feature = True\n")
                git(root, "add", "consumer.py")
                git(root, "commit", "-m", "related upstream change")
                changed.append(git(root, "rev-parse", "HEAD"))
                self.with_finding = False
            return result

        driver.launcher = advance_related_base
        result = driver.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches["implement"], 1)
        self.assertEqual(self.launches["reviewer-prompt.md"], 2)
        with fixture_database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
            self.assertEqual(entry["request"]["base_refresh_count"], 1)
            self.assertEqual(entry["manifest"]["base_sha"], changed[0])
            self.assertIn("value = 4", Path(driver.assigned_path, "source.py").read_text())
            self.assertIn("base_feature = True", Path(driver.assigned_path, "consumer.py").read_text())

    def test_single_reviewer_and_processing_gate_result(self):
        driver = self.start(["claude"])
        early = submission.record_result(self.db, driver.envelope(self.submission))
        self.assertEqual(early["reason_code"], "pre_pr_review_required")
        result = driver.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(driver.manifest["decision_owner"]["login"], "maintainer")
        reviewer_prompt = (Path(driver.manifest["artifact_root"]) / "reviewer-prompt.md").read_text()
        self.assertIn('"decision_mention": "@maintainer"', reviewer_prompt)
        self.assertNotIn("user", json.loads((Path(driver.manifest["context_dir"]) / "pr-info.json").read_text()))
        self.assertEqual(self.launches, {"implement": 1, "reviewer-prompt.md": 1, "address": 1})
        with fixture_database.connect(self.db) as conn:
            saved = store.session(conn, task_id=driver.task_id)
            self.assertEqual(saved["final_result_id"], result["result_id"])
            self.assertEqual(saved["proof"]["head_sha"], saved["manifest"]["head_sha"])
            (body,) = conn.execute("SELECT metadata_json FROM github_actions WHERE action_type='open_pr'").fetchone()
            self.assertIn("未处理及原因", json.loads(body)["body"])
            self.assertEqual(queries.ready_actions(conn, 10), [])  # Pending normal audit.

    def test_pre_pr_review_preserves_a_skipped_optional_check(self):
        driver = self.start(["claude"])
        skipped = _verification_evidence("optional-smoke", status="skipped", required=False)
        self.verification = [*self.verification, skipped]
        result = driver.run()
        self.assertTrue(result["ok"], result)
        self.cycle([])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(
                [x["action_type"] for x in queries.ready_actions(conn, 10)], ["push_existing_pr", "open_pr"]
            )
            (metadata,) = conn.execute("SELECT metadata_json FROM worker_results").fetchone()
            self.assertIn({**skipped, "attested": False}, json.loads(metadata)["normalized_payload"]["verification"])

    def test_failed_synthesis_reuses_both_reviewers(self):
        def launch(**kwargs):
            return {**self.launch_pr_stage(**kwargs), "prompt_path": str(kwargs["prompt_path"])}

        driver = self.start(["codex", "claude"])
        driver.launcher = launch
        self.fail_synthesis = True
        wait = driver.stopping.wait

        def interrupt_after_failure(seconds):
            if not self.fail_synthesis:
                raise InterruptedError
            return wait(seconds)

        with (
            patch.object(driver.stopping, "wait", side_effect=interrupt_after_failure),
            self.assertRaises(InterruptedError),
        ):
            driver.run()
        self.assertEqual(self.launches["synthesis"], 1)
        restarted = Coordinator(self.db, driver.task_id, driver.attempt_id, launcher=launch, poll_seconds=0.01)
        with fixture_database.connect(self.db) as conn:
            store.update(conn, restarted.round_id, next_retry_at=None)
        prompt = restarted.prompt

        def upgraded_prompt(name, text, **kwargs):
            if name.startswith("synthesis-prompt-"):
                text += "\nUpdated deployment guidance.\n"
            return prompt(name, text, **kwargs)

        with (
            patch.object(review_workspace, "_reviewer_text", return_value="Updated reviewer template"),
            patch.object(restarted, "prompt", side_effect=upgraded_prompt),
            patch.object(restarted.stopping, "wait", side_effect=AssertionError("unexpected retry after upgrade")),
        ):
            result = restarted.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            self.launches,
            {
                "implement": 1,
                "reviewer-prompt.md": 2,
                "synthesis": 2,
                "address": 1,
            },
        )
        with fixture_database.connect(self.db) as conn:
            syntheses = [run for run in store.runs(conn, driver.round_id) if run["stage"] == "synthesis"]
        self.assertEqual([run["status"] for run in syntheses], ["failed", "succeeded"])
        self.assertEqual(syntheses[0]["prompt_sha256"], syntheses[1]["prompt_sha256"])
        saved_prompt = Path(syntheses[0]["dispatch"]["prompt_path"])
        with self.assertRaisesRegex(ValueError, "registered phase prompt changed"):
            restarted.prompt(saved_prompt.name, "New template", stage="prepare")
        saved_prompt.write_text("Tampered contents")
        with self.assertRaisesRegex(ValueError, "registered phase prompt changed"):
            restarted.prompt(saved_prompt.name, "New template", stage="synthesis")

    def test_failed_disposition_blocks_all_publication(self):
        driver = self.start(["claude"])
        self.fail_disposition = True
        with patch.object(driver.stopping, "wait", return_value=False), self.assertRaises(OSError):
            driver.run()
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0], 0)
            self.assertEqual(store.session(conn, task_id=driver.task_id)["status"], "attention")
        self.assertEqual(self.launches["address"], 3)
        self.assertEqual(self.launches["reviewer-prompt.md"], 1)

    def test_final_repaired_sha_and_review_body_are_published_in_order(self):
        driver = self.start(["claude"])
        self.with_finding = True
        remote_url = "https://github.com/example/backend.git"
        git(driver.assigned_path, "remote", "add", "origin", remote_url)
        recorded = driver.run()
        self.assertTrue(recorded["ok"], recorded)
        self.cycle([])
        with fixture_database.connect(self.db) as conn:
            session = store.session(conn, task_id=driver.task_id)
            ready = queries.ready_actions(conn, 10)
        self.assertEqual([action["action_type"] for action in ready], ["push_existing_pr", "open_pr"])
        proof = session["proof"]
        self.assertNotEqual(proof["head_sha"], proof["candidate_sha"])
        effects = []
        body = None
        url = "https://github.com/example/backend/pull/43"

        def remote_command(cmd, **_kwargs):
            nonlocal body

            def result(output=""):
                return SimpleNamespace(returncode=0, stdout=output, stderr="")

            if cmd[0:3] == ["git", "remote", "get-url"] or cmd[0:3] == ["git", "ls-remote", "--get-url"]:
                return result(remote_url + "\n")
            if cmd[0:3] == ["git", "ls-remote", "--exit-code"]:
                ref = cmd[-1]
                sha = proof["base_sha"] if ref.endswith("/main") else proof["head_sha"]
                return result(f"{sha}\t{ref}\n")
            if cmd[0:3] == ["git", "ls-remote", "--heads"]:
                return result()
            if cmd[0:2] == ["git", "push"]:
                self.assertEqual(cmd[-1], f"{proof['head_sha']}:refs/heads/{driver.branch}")
                effects.append("push")
                return result()
            if cmd[0:3] == ["gh", "pr", "list"]:
                return result("[]")
            if cmd[0:3] == ["gh", "pr", "create"]:
                effects.append("open_pr")
                body = cmd[cmd.index("--body") + 1]
                return result(url + "\n")
            if cmd[0:2] == ["gh", "api"] and cmd[2].endswith("/pulls/43"):
                return result(
                    json.dumps(
                        {
                            "number": 43,
                            "html_url": url,
                            "body": body,
                            "head": {
                                "sha": proof["head_sha"],
                                "ref": driver.branch,
                                "repo": {"full_name": "example/backend"},
                            },
                            "base": {"sha": proof["base_sha"], "ref": "main", "repo": {"full_name": "example/backend"}},
                        }
                    )
                )
            self.fail(f"unexpected publication command {cmd}")

        first = publish.publish_ready_actions(self.db, run_command=remote_command, limit=1)
        second = publish.publish_ready_actions(self.db, run_command=remote_command)
        self.assertEqual(first["published_count"], 1, first)
        self.assertEqual(second["published_count"], 1, second)
        self.assertEqual(effects, ["push", "open_pr"])
        self.assertIn("Review 报告", body)
        self.assertIn("F1：Updated the value", body)
        self.assertIn(proof["head_sha"], body)

    def test_web_pr_requires_review_and_freezes_manually_selected_worker(self):
        config = yaml.safe_load(self.config_path.read_text())
        config["repos"][0]["routes"] = {"new-pr": {"pre_pr_review": {"workers": ["codex"]}}}
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([])
        repo_id = "repo:example/backend"
        context = commands.CommandContext("operator", "owner", frozenset({repo_id}), frozenset({"codex", "claude"}))
        created = commands.create_work_item(
            self.db,
            context=context,
            repo_id=repo_id,
            title="Fix example bug",
            description="Please fix this bug in the source file.",
            priority="P1",
            routing_mode="manual",
            requested_worker="claude",
            start=True,
            idempotency_key="manual-reviewed-new-pr",
        )
        self.cycle([])
        task, attempt = self.current()
        with fixture_database.connect(self.db) as conn:
            entry = store.session(conn, task_id=task)
            self.assertEqual(entry["request"]["plan"]["implementer"]["name"], "claude")
            self.assertEqual([worker["name"] for worker in entry["request"]["plan"]["workers"]], ["codex"])
        refused = submission.record_result(
            self.db,
            {"task_id": task, "attempt_id": attempt, "output_type": "new_pr", "submission": {}},
        )
        self.assertEqual(refused["reason_code"], "pre_pr_review_required")

        item_id = created["item"]["work_item_id"]
        with fixture_database.connect(self.db) as conn:
            run = store.new_run(conn, entry["round_id"], "implement", "claude", attempt, self.root, "prompt")
            store.update_run(conn, run["run_id"], status="waiting")
            self.assertTrue(store.reserve_slot(conn, run["run_id"]))
            conn.execute("UPDATE attempts SET status='failed' WHERE attempt_id=?", (attempt,))
            conn.execute("UPDATE tasks SET lifecycle='failed' WHERE task_id=?", (task,))
            version = conn.execute("SELECT version FROM work_items WHERE work_item_id=?", (item_id,)).fetchone()[0]

        with self.assertRaisesRegex(commands.WorkItemConflictError, "processes before retrying"):
            commands.execute_command(
                self.db,
                item_id,
                context=context,
                command="retry",
                expected_version=version,
                idempotency_key="blocked-review-retry",
            )
        with fixture_database.connect(self.db) as conn:
            store.update_run(conn, run["run_id"], status="interrupted", slot_reserved=0)
        del config["repos"][0]["routes"]
        self.config_path.write_text(yaml.safe_dump(config))
        started = commands.execute_command(
            self.db,
            item_id,
            context=context,
            command="retry",
            expected_version=version,
            idempotency_key="review-retry-with-config-disabled",
        )
        self.assertTrue(started["ok"], started)
        self.cycle([])
        new_task, new_attempt = self.current()
        self.assertNotEqual(new_task, task)
        with fixture_database.connect(self.db) as conn:
            restarted = store.session(conn, task_id=new_task)
            self.assertIsNotNone(restarted)
            self.assertEqual([worker["name"] for worker in restarted["request"]["plan"]["workers"]], ["codex"])
        refused = submission.record_result(
            self.db,
            {"task_id": new_task, "attempt_id": new_attempt, "output_type": "new_pr", "submission": {}},
        )
        self.assertEqual(refused["reason_code"], "pre_pr_review_required")

    def test_ambiguous_writer_start_retains_task_slot_and_not_model_slot(self):
        driver = self.start(["claude"])
        with fixture_database.connect(self.db) as conn:
            run = store.new_run(
                conn, driver.round_id, "implement", "codex", driver.attempt_id, self.root, "prompt", model="default"
            )
            store.update_run(conn, run["run_id"], status="waiting")
            self.assertTrue(store.reserve_slot(conn, run["run_id"]))
            conn.execute("UPDATE attempts SET status='failed' WHERE attempt_id=?", (driver.attempt_id,))
            conn.commit()
            self.assertEqual(capacity.running_attempt_count(conn, "repo:example/backend"), 1)
            self.assertEqual(pool.status(conn)["in_use"], 0)
            self.assertFalse(rounds.observe_exit(conn, driver.task_id)["all_stopped"])
