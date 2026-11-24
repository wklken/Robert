"""Pre-publication decisions remain deliverable and resumable without a PR."""

import json
from pathlib import Path
from unittest.mock import patch
from robert_agent.execution.stages import StageStopped

from robert_agent.adapters.git.review_revision import git
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.workflows.pre_pr_review.coordinator import Coordinator
from tests.support import database
from tests.support.pre_pr_review import PrePRReviewFixture


class PrePRDecisionTests(PrePRReviewFixture):
    def question_launcher(self, driver):
        launch = driver.launcher

        def question(**kwargs):
            if "ROBERT_PRE_PR_CANDIDATE_DIR" not in kwargs["trusted_environment"]:
                return launch(**kwargs)
            self.launches["implement"] += 1
            payload = {
                **self.submission,
                "planned_github_actions": [],
                "verification": [],
                "operator_question": {
                    "kind": "scope_decision",
                    "summary": "Choose the credential compatibility behavior.",
                    "choices": self.question_choices,
                },
            }
            if driver.origin_type != "web":
                payload["planned_github_actions"] = [
                    {"type": "comment", "body": "Choose the credential compatibility behavior."}
                ]
            output = Path(kwargs["log_dir"])
            (output / "candidate.json").write_text(
                json.dumps(
                    {
                        "task_id": driver.task_id,
                        "attempt_id": driver.attempt_id,
                        "output_type": "waiting_for_user",
                        "submission": payload,
                    }
                )
            )
            return {"ok": True, "process_exited": True, "returncode": 0, "pid": 1073741824}

        return question

    def test_implementation_question_is_registered_before_candidate_and_without_review(self):
        driver = self.start(["claude"], tapd=True)
        question = {
            "kind": "scope_decision",
            "summary": "Choose credential compatibility.",
            "choices": self.question_choices,
        }
        payload = {
            **self.submission,
            "planned_github_actions": [{"type": "comment", "body": question["summary"]}],
            "verification": [],
            "operator_question": question,
        }
        output = self.root / "question-output"
        output.mkdir()
        (output / "candidate.json").write_text(
            json.dumps(
                {
                    "task_id": driver.task_id,
                    "attempt_id": driver.attempt_id,
                    "output_type": "waiting_for_user",
                    "submission": payload,
                }
            )
        )
        artifact = driver.validate_candidate(output, "model")
        self.assertEqual(artifact["output_type"], "waiting_for_user")
        self.assertEqual(artifact["operator_question"], question)

    def test_base_change_does_not_swallow_a_bound_review_decision(self):
        driver = self.start(["claude"])
        self.with_finding = self.needs_decision = True
        launch = driver.launcher
        changed = []

        def advance(**kwargs):
            result = launch(**kwargs)
            if Path(kwargs["prompt_path"]).name.startswith("address-") and not changed:
                tree = self.git_fixture.upstream
                git(tree, "checkout", "main")
                (tree / "consumer.py").write_text("from source import value\nnew_contract = True\n")
                git(tree, "add", "consumer.py")
                git(tree, "commit", "-m", "upstream runtime change")
                changed.append(git(tree, "rev-parse", "HEAD"))
            return result

        driver.launcher = advance
        result = driver.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches["address"], 1)
        with database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
            self.assertEqual(entry["status"], "waiting_for_user")
            self.assertEqual(entry["proof"]["decision"]["observed_base_sha"], changed[0])
            self.assertNotEqual(entry["manifest"]["base_sha"], changed[0])
            self.assertNotIn("base_refresh_count", entry["request"])
            self.assertTrue(all(r["status"] != "running" for r in store.runs(conn, driver.round_id)))

    def test_new_pr_audit_rejection_preserves_specific_safe_reason(self):
        driver = self.start(["claude"])
        invalid = {**self.submission, "planned_github_actions": [{"type": "comment", "body": "Only a question."}]}
        with self.assertRaisesRegex(ValueError, "new_pr requires planned_github_actions"):
            driver.validate_submission(invalid)

    def test_implementation_question_is_published_and_reuses_the_round_after_reply(self):
        driver = self.start(["claude"], local=True)
        driver.launcher = self.question_launcher(driver)
        result = driver.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches["implement"], 1)
        self.cycle([])
        from robert_agent.work.application import commands

        with database.connect(self.db) as conn:
            item = conn.execute("SELECT work_item_id,version FROM work_items").fetchone()
            question = conn.execute(
                "SELECT event_id FROM work_item_events WHERE event_type='operator_question'"
            ).fetchone()
            entry = store.session(conn, task_id=driver.task_id)
            self.assertEqual(entry["proof"]["decision"]["stage"], "implement")
            self.assertEqual(store.runs(conn, driver.round_id)[0]["status"], "succeeded")
        context = commands.CommandContext(
            "operator", "owner", frozenset({"repo:example/backend"}), frozenset({"codex", "claude"})
        )
        commands.execute_command(
            self.db,
            item[0],
            context=context,
            command="reply",
            expected_version=item[1],
            expected_event_id=question[0],
            idempotency_key="implementation-reply",
            body="Use the explicit supported compatibility mode.",
        )
        self.cycle([])
        task, attempt = self.current()
        resumed = Coordinator(self.db, task, attempt, launcher=self.launch_pr_stage, poll_seconds=0.01)
        self.assertEqual(resumed.round_id, driver.round_id)
        self.assertNotEqual(resumed.request["prompt_sha256"], driver.request["prompt_sha256"])
        result = resumed.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches["implement"], 2)
        self.assertEqual(self.launches["reviewer-prompt.md"], 1)

    def test_question_registration_failure_reuses_sealed_question_without_model(self):
        driver = self.start(["claude"], local=True)
        driver.launcher = self.question_launcher(driver)
        with patch(
            "robert_agent.workflows.pre_pr_review.decisions.submission.record_result",
            return_value={"ok": False, "status": "validation_unavailable"},
        ):
            with self.assertRaisesRegex(StageStopped, "question submission blocked"):
                driver.run()
        self.assertEqual(self.launches["implement"], 1)
        retry = Coordinator(
            self.db, driver.task_id, driver.attempt_id, launcher=self.question_launcher(driver), poll_seconds=0.01
        )
        result = retry.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches["implement"], 1)

    def test_question_source_mutation_remains_blocked(self):
        driver = self.start(["claude"], local=True)
        driver.launcher = self.question_launcher(driver)
        with patch(
            "robert_agent.workflows.pre_pr_review.decisions.submission.record_result",
            return_value={"ok": False, "status": "validation_unavailable"},
        ):
            with self.assertRaises(StageStopped):
                driver.run()
        (Path(driver.assigned_path) / "source.py").write_text("value = 999\n")
        retry = Coordinator(
            self.db, driver.task_id, driver.attempt_id, launcher=self.question_launcher(driver), poll_seconds=0.01
        )
        with self.assertRaisesRegex(StageStopped, "question source worktree changed"):
            retry.run()
        with database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 0)
        self.assertEqual(self.launches["implement"], 1)

    def test_worker_saved_candidate_retains_the_waiting_output_type(self):
        import io
        import os
        from robert_agent.worker import result as worker_result

        driver = self.start(["claude"], local=True)
        target = self.root / "worker-output"
        target.mkdir()
        payload = {
            **self.submission,
            "planned_github_actions": [],
            "verification": [],
            "operator_question": {
                "kind": "scope_decision",
                "summary": "Choose compatibility.",
                "choices": self.question_choices,
            },
        }
        with (
            patch.dict(os.environ, {"ROBERT_PRE_PR_CANDIDATE_DIR": str(target)}),
            patch("sys.stdin", io.StringIO(json.dumps(payload))),
            patch("sys.stdout", io.StringIO()),
        ):
            worker_result.main(
                [
                    "--db",
                    str(self.db),
                    "--task-id",
                    driver.task_id,
                    "--attempt-id",
                    driver.attempt_id,
                    "--output-type",
                    "waiting_for_user",
                ]
            )
        saved = json.loads((target / "candidate.json").read_text())
        self.assertEqual(saved["output_type"], "waiting_for_user")
        artifact = driver.validate_candidate(target, "model")
        self.assertEqual(artifact["operator_question"], payload["operator_question"])

    def test_implementation_reply_rejects_changed_saved_question(self):
        driver = self.start(["claude"], local=True)
        driver.launcher = self.question_launcher(driver)
        result = driver.run()
        self.assertTrue(result["ok"], result)
        self.cycle([])
        from robert_agent.work.application import commands

        with database.connect(self.db) as conn:
            item = conn.execute("SELECT work_item_id,version FROM work_items").fetchone()
            question = conn.execute(
                "SELECT event_id FROM work_item_events WHERE event_type='operator_question'"
            ).fetchone()
            binding = store.session(conn, task_id=driver.task_id)["proof"]["decision"]
        Path(binding["candidate_path"]).write_text("{}")
        context = commands.CommandContext(
            "operator", "owner", frozenset({"repo:example/backend"}), frozenset({"codex", "claude"})
        )
        with self.assertRaisesRegex(commands.WorkItemConflictError, "question checkpoint changed"):
            commands.execute_command(
                self.db,
                item[0],
                context=context,
                command="reply",
                expected_version=item[1],
                expected_event_id=question[0],
                idempotency_key="forged-implementation-reply",
                body="Continue with the original mode.",
            )

    def test_implementation_missing_choices_does_not_start_review_or_publish(self):
        driver = self.start(["claude"], local=True)
        driver.launcher = self.question_launcher(driver)
        self.question_choices = []
        with (
            patch.object(driver.stopping, "wait", return_value=False),
            self.assertRaisesRegex(ValueError, "two to five"),
        ):
            driver.run()
        self.assertEqual(self.launches["implement"], 3)
        self.assertEqual(self.launches["reviewer-prompt.md"], 0)
        with database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 0)

    def assert_wrong_envelope_owner(self, field):
        driver = self.start(["claude"], local=True)
        launch = self.question_launcher(driver)

        def forged(**kwargs):
            result = launch(**kwargs)
            path = Path(kwargs["log_dir"]) / "candidate.json"
            value = json.loads(path.read_text())
            value[field] = "different-owner"
            path.write_text(json.dumps(value))
            return result

        driver.launcher = forged
        with (
            patch.object(driver.stopping, "wait", return_value=False),
            self.assertRaisesRegex(StageStopped, "candidate result owner"),
        ):
            driver.run()
        with database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 0)

    def test_implementation_envelope_rejects_wrong_task(self):
        self.assert_wrong_envelope_owner("task_id")

    def test_implementation_envelope_rejects_wrong_attempt(self):
        self.assert_wrong_envelope_owner("attempt_id")

    def assert_changed_question_evidence(self, field):
        driver = self.start(["claude"], local=True)
        driver.launcher = self.question_launcher(driver)
        observed = driver.observe_base

        def mutate(*args):
            base = observed(*args)
            with database.connect(self.db) as conn:
                run = next(r for r in store.runs(conn, driver.round_id) if r["stage"] == "implement")
            path = (
                Path(driver.assigned_path) / "source.py"
                if field == "writer"
                else Path(run["artifact"]["candidate_path"])
            )
            path.write_bytes(path.read_bytes() + b"\n")
            return base

        driver.observe_base = mutate
        with self.assertRaises(StageStopped):
            driver.run()
        with database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 0)

    def test_question_rechecks_writer_after_base_observation(self):
        self.assert_changed_question_evidence("writer")

    def test_question_rechecks_artifact_after_base_observation(self):
        self.assert_changed_question_evidence("candidate")

    def test_final_question_rechecks_feedback_after_base_observation(self):
        driver = self.start(["claude"], local=True)
        self.with_finding = True
        self.final_review_mode = "blocking_approval"
        observed = driver.observe_base
        mutated = []

        def mutate(*args):
            base = observed(*args)
            if driver.final_decision_feedback:
                path = Path(driver.final_decision_feedback["verification_path"])
                path.write_text("{}")
                mutated.append(path)
            return base

        driver.observe_base = mutate
        with self.assertRaises(StageStopped):
            driver.run()
        self.assertEqual(len(mutated), 1)
        with database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 0)

    def test_saved_envelope_keeps_original_owner_when_reused_by_a_later_attempt(self):
        from robert_agent.workflows.pre_pr_review import decisions

        driver = self.start(["claude"], local=True)
        driver.launcher = self.question_launcher(driver)
        self.assertTrue(driver.run()["ok"])
        with database.connect(self.db) as conn:
            run = next(r for r in store.runs(conn, driver.round_id) if r["stage"] == "implement")
        path = Path(run["artifact"]["candidate_path"])
        saved = json.loads(path.read_text())
        driver.task_id, driver.attempt_id = "later-task", "later-attempt"
        kind, payload = decisions.candidate_payload(driver, saved, source_path=path)
        self.assertEqual(kind, "waiting_for_user")
        self.assertEqual(payload, saved["submission"])

    def test_unavailable_base_is_question_context_without_code_publication(self):
        driver = self.start(["claude"], local=True)
        driver.launcher = self.question_launcher(driver)
        driver.observe_base = lambda *_: (_ for _ in ()).throw(OSError("remote observation unavailable"))
        self.assertTrue(driver.run()["ok"])
        with database.connect(self.db) as conn:
            binding = store.session(conn, task_id=driver.task_id)["proof"]["decision"]
            self.assertIsNone(binding["observed_base_sha"])
            self.assertIn("remote observation unavailable", binding["base_observation_error"])
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM github_actions WHERE action_type='open_pr'").fetchone()[0], 0
            )

    def test_stopped_base_observation_does_not_submit_a_question(self):
        driver = self.start(["claude"], local=True)
        driver.launcher = self.question_launcher(driver)
        driver.observe_base = lambda *_: (_ for _ in ()).throw(StageStopped("operator stopped coordinator"))
        with self.assertRaisesRegex(StageStopped, "operator stopped coordinator"):
            driver.run()
        with database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 0)
