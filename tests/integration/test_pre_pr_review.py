from collections import Counter
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from robert_agent.adapters.git.review_revision import git
from robert_agent.configuration import validation as config_validation
from robert_agent.delivery import queries
from robert_agent.delivery.application import publish, submission, validation
from robert_agent.execution import capacity, rounds
from robert_agent.execution.stages import StageStopped
from robert_agent.persistence.rounds import pool
from robert_agent.work.application import commands, tapd_scan
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.workflows.pre_pr_review.coordinator import Coordinator
from robert_agent.workflows.review import workspace as review_workspace
from tests.support.work_items import example_choices
from tests.support import database as fixture_database
from tests.support.repository_cycle import _new_pr_actions, _verification_evidence
from tests.support.review_coordinator import ReviewCoordinatorFixture
from tests.support.tapd import STORY, FakeClient, comment
from tests.support.tapd_cycle import TAPD_CONFIG


class PrePublicationReviewTests(ReviewCoordinatorFixture):
    def test_invalid_handoff_repairs_only_the_artifact_and_reuses_implementation(self):
        driver = self.start(["claude"])
        launch = driver.launcher
        repairs = []

        def malformed_then_correct(**kwargs):
            prompt = Path(kwargs["prompt_path"])
            output = Path(kwargs["log_dir"])
            if prompt.name.startswith("repair-handoff-"):
                inputs = json.loads(
                    Path(kwargs["trusted_environment"]["ROBERT_PRE_PR_HANDOFF_REPAIR_INPUT"]).read_text()
                )
                self.assertIn("unsupported", inputs["error"])
                payload = json.loads(Path(inputs["handoff_path"]).read_text())
                payload.pop("unexpected_program_field")
                (output / "handoff.json").write_text(json.dumps(payload))
                repairs.append(inputs)
                return {"ok": True, "process_exited": True, "returncode": 0, "pid": 1073741824}
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
        self.assertEqual(len(repairs), 1)

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

    def start(self, workers, *, local=False, tapd=False, github_comment=False):
        config = yaml.safe_load(self.config_path.read_text())
        config["repos"][0]["routes"] = {"new-pr": {"pre_pr_review": {"workers": workers}}}
        if tapd:
            config.update(yaml.safe_load(TAPD_CONFIG.format(trusted="alice")))
        self.config_path.write_text(yaml.safe_dump(config))
        if tapd:
            self.cycle([])
            settings = config_validation.validate_config(self.config_path, skip_external=True)
            client = FakeClient()
            client.comments.append(comment("2", "@robert-wklken please fix this bug"))
            tapd_scan.scan_project(
                self.db,
                settings["tapd"],
                settings["tapd"]["projects"][0],
                {repo["full_name"]: repo for repo in settings["repos"]},
                client,
                now="2026-11-20T03:00:00+00:00",
            )
            self.cycle([])
        elif local:
            self.cycle([])
            context = commands.CommandContext(
                "operator", "owner", frozenset({"repo:example/backend"}), frozenset({"codex", "claude"})
            )
            commands.create_work_item(
                self.db,
                context=context,
                repo_id="repo:example/backend",
                title="Fix example bug",
                description="Please fix this bug in the source file.",
                priority="P1",
                routing_mode="manual",
                requested_worker="codex",
                start=True,
                idempotency_key="local-question-source",
            )
            self.cycle([])
        else:
            self.cycle(
                [
                    {
                        "id": "implement",
                        "number": 42,
                        "source_type": "issue",
                        "event_type": "comment" if github_comment else "assigned",
                        "actor_login": "maintainer",
                        "assigned_to": "robert-bot",
                        "assignment_actor_login": "maintainer",
                        "body": "@robert-bot update the example" if github_comment else "Update the example",
                        "intent": "small_change",
                        "title": "Update example",
                        "state": "open",
                        "html_url": "https://github.com/example/backend/issues/42"
                        + ("#issuecomment-123" if github_comment else ""),
                    }
                ]
            )
        task, attempt = self.current()
        with fixture_database.connect(self.db) as conn:
            saved = store.session(conn, task_id=task)
        self.assertIsNotNone(saved)
        self.verification = [_verification_evidence()]
        self.submission = {
            "planned_github_actions": _new_pr_actions(),
            "verification": self.verification,
            "used_skills": [],
            "consumed_event_fingerprints": [saved["request"]["event"]["event_fingerprint"]] if not local else [],
            "handoff": "Update example and verify the result.",
        }
        if tapd:
            self.submission["planned_github_actions"].append({"type": "comment", "body": "Updated the example."})
        if local:
            with fixture_database.connect(self.db) as conn:
                self.submission["consumed_work_item_event_ids"] = [
                    row[0] for row in conn.execute("SELECT event_id FROM work_item_events")
                ]
        driver = Coordinator(self.db, task, attempt, launcher=self.launch_pr_stage, poll_seconds=0.01)
        if not Path(driver.assigned_path).exists():
            Path(driver.assigned_path).parent.mkdir(parents=True, exist_ok=True)
            git(
                driver.repo["repo_root"],
                "worktree",
                "add",
                "-b",
                driver.branch,
                driver.assigned_path,
                self.git_fixture.base,
            )
        git(driver.assigned_path, "config", "user.name", "Test")
        git(driver.assigned_path, "config", "user.email", "test@example.invalid")
        self.fail_synthesis = False
        self.fail_disposition = False
        self.with_finding = False
        self.needs_decision = False
        self.question_choices = example_choices()
        self.launches = Counter()
        self.final_review_mode = "approved"
        return driver

    def launch_pr_stage(self, **kwargs):
        prompt = Path(kwargs["prompt_path"])
        output = Path(kwargs["log_dir"])
        tree = Path(kwargs["worktree_path"])
        if "ROBERT_PRE_PR_CANDIDATE_DIR" in kwargs["trusted_environment"]:
            self.launches["implement"] += 1
            (tree / "source.py").write_text("value = 3\n")
            git(tree, "commit", "-am", "implement")
            (output / "candidate.json").write_text(json.dumps(self.submission))
        elif prompt.name.startswith("verify-final-"):
            self.launches["verify_final"] += 1
            value = json.loads(Path(kwargs["trusted_environment"]["ROBERT_PRE_PR_FINAL_REVIEW_INPUT"]).read_text())
            self.assertEqual(git(tree, "rev-parse", "HEAD"), value["final_sha"])
            self.assertIn("value = 4", (tree / "source.py").read_text())
            self.assertIn("value = 3", Path(value["additional_diff_path"]).read_text())
            if self.final_review_mode == "failed":
                return {"ok": True, "process_exited": True, "returncode": 1, "pid": 1073741824}
            result = {
                key: value[key]
                for key in (
                    "round_id",
                    "snapshot_id",
                    "candidate_sha",
                    "final_sha",
                    "report_sha256",
                    "dispositions_sha256",
                    "additional_diff_sha256",
                )
            }
            result.update(
                schema_version=1,
                recommendation="approved",
                blocking_findings=[],
                checked_findings=[
                    {"id": item, "status": "resolved", "reason": "Verified final code and new diff"}
                    for item in value["finding_ids"]
                ],
            )
            if self.final_review_mode == "wrong_sha":
                result["final_sha"] = value["candidate_sha"]
            elif self.final_review_mode in {"blocked", "unknown"}:
                result["recommendation"] = self.final_review_mode
            elif self.final_review_mode == "blocking_approval":
                result["checked_findings"][0]["status"] = "blocking"
            (output / "final-verification.json").write_text(json.dumps(result))
        elif prompt.name.startswith("address-"):
            self.launches["address"] += 1
            if not self.fail_disposition:
                report_sha = prompt.stem.removeprefix("address-").split("-")[0]
                candidate_sha = json.loads((prompt.parent / "context" / "manifest.json").read_text())["head_sha"]
                decisions = []
                if self.with_finding:
                    if (tree / "source.py").read_text() != "value = 4\n":
                        (tree / "source.py").write_text("value = 4\n")
                        git(tree, "commit", "-am", "address finding")
                    decisions = [
                        {"id": "F1", "status": "fixed", "reason": "Updated the value and verified its consumer."}
                    ]
                if self.needs_decision:
                    decisions = [
                        {
                            "id": "F1",
                            "status": "needs_decision",
                            "reason": "Confirm expected value within the original scope.",
                        }
                    ]
                (output / "dispositions.json").write_text(
                    json.dumps(
                        {
                            "candidate_sha": candidate_sha,
                            "final_sha": git(tree, "rev-parse", "HEAD"),
                            "report_sha256": report_sha,
                            "dispositions": decisions,
                            **({"choices": self.question_choices} if self.needs_decision else {}),
                            "verification": self.verification,
                            "body": "Updated example.\n\n## Assumptions and decisions\nNone.",
                        }
                    )
                )
        else:
            if prompt.name.startswith("synthesis-"):
                self.launches["synthesis"] += 1
                if self.fail_synthesis:
                    self.fail_synthesis = False
                    with fixture_database.connect(self.db) as conn:
                        self.assertEqual(queries.ready_actions(conn, 10), [])
                        self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 0)
                    return {"ok": True, "process_exited": True, "returncode": 1, "pid": 1073741824}
            else:
                self.launches[prompt.name] += 1
            launched = self.launcher(**kwargs)
            if self.with_finding and prompt.name == "reviewer-prompt.md":
                path = output / "result.json"
                value = json.loads(path.read_text())
                value["findings"] = [
                    {
                        "id": "F1",
                        "title": "Unexpected value",
                        "severity": "medium",
                        "location": {"path": "source.py", "line": 1},
                        "trigger": "Loading the module",
                        "impact": "The example value differs",
                        "evidence": "The assignment uses the wrong value",
                        "suggestion": "Use the expected value",
                    }
                ]
                path.write_text(json.dumps(value))
            return launched
        return {"ok": True, "process_exited": True, "returncode": 0, "pid": 1073741824}

    def test_review_decision_records_only_a_bound_question(self):
        driver = self.start(["claude"])
        self.with_finding = self.needs_decision = True
        result = driver.run()
        self.assertTrue(result["ok"], result)
        with fixture_database.connect(self.db) as conn:
            saved = store.session(conn, task_id=driver.task_id)
            question = json.loads(conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0])[
                "operator_question"
            ]
            self.assertEqual(question["kind"], "scope_decision")
            self.assertEqual(question["choices"], example_choices())
            self.assertEqual(saved["proof"]["decision"]["round_id"], driver.round_id)
            self.assertEqual(saved["proof"]["decision"]["candidate_sha"], saved["manifest"]["head_sha"])
            self.assertEqual(saved["proof"]["decision"]["report_sha256"], saved["report_sha256"])
            self.assertEqual(saved["status"], "waiting_for_user")
            self.assertFalse(any(run["slot_reserved"] for run in store.runs(conn, driver.round_id)))
            actions = conn.execute("SELECT action_type FROM github_actions").fetchall()
            self.assertEqual([row[0] for row in actions], ["comment"])
        self.assertEqual(self.launches["address"], 1)

    def test_tapd_review_decision_targets_the_ticket_without_a_github_event_url(self):
        driver = self.start(["claude"], tapd=True)
        self.assertNotIn("html_url", driver.request["event"])
        self.with_finding = self.needs_decision = True

        result = driver.run()

        self.assertTrue(result["ok"], result)
        with fixture_database.connect(self.db) as conn:
            saved = store.session(conn, task_id=driver.task_id)
            self.assertEqual(saved["status"], "waiting_for_user")
            self.assertEqual(saved["proof"]["decision"]["candidate_sha"], saved["manifest"]["head_sha"])
            self.assertFalse(any(run["slot_reserved"] for run in store.runs(conn, driver.round_id)))
            question = json.loads(conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0])[
                "operator_question"
            ]
            self.assertEqual(question["choices"], example_choices())
            actions = conn.execute("SELECT action_type, target_url FROM github_actions").fetchall()
            self.assertEqual(
                [tuple(row) for row in actions],
                [("comment", f"https://tapd.example/100/prong/stories/view/{STORY}")],
            )

    def test_github_comment_review_decision_targets_the_issue_without_the_comment_fragment(self):
        driver = self.start(["claude"], github_comment=True)
        self.with_finding = self.needs_decision = True

        result = driver.run()

        self.assertTrue(result["ok"], result)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(store.session(conn, task_id=driver.task_id)["status"], "waiting_for_user")
            actions = conn.execute("SELECT action_type, target_url FROM github_actions").fetchall()
            self.assertEqual(
                [tuple(row) for row in actions], [("comment", "https://github.com/example/backend/issues/42")]
            )

    def test_review_missing_choices_stays_in_recovery_and_does_not_publish_an_unanswerable_question(self):
        driver = self.start(["claude"])
        self.with_finding = self.needs_decision = True
        self.question_choices = []
        with (
            patch.object(driver.stopping, "wait", return_value=False),
            self.assertRaisesRegex(ValueError, "two to five"),
        ):
            driver.run()
        with fixture_database.connect(self.db) as conn:
            saved = store.session(conn, task_id=driver.task_id)
            self.assertEqual(saved["status"], "attention")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 0)
            self.assertTrue(
                all(
                    run["status"] == "failed"
                    for run in store.runs(conn, driver.round_id)
                    if run["stage"] == "address_findings"
                )
            )

    def test_web_review_decision_has_no_publication_and_resumes(self):
        driver = self.start(["claude"], local=True)
        self.with_finding = self.needs_decision = True
        recorded = driver.run()
        self.assertTrue(recorded["ok"], recorded)
        self.cycle([])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0], 0)
            self.assertEqual(capacity.running_attempt_count(conn, "repo:example/backend"), 0)
            item = conn.execute("SELECT work_item_id,version FROM work_items").fetchone()
            question = conn.execute(
                "SELECT event_id FROM work_item_events WHERE event_type='operator_question'"
            ).fetchone()
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
            idempotency_key="web-review-reply",
            body="Use the expected value within the original scope.",
        )
        self.needs_decision = False
        self.cycle([])
        task, attempt = self.current()
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(store.session(conn, task_id=task)["round_id"], driver.round_id)
        self.assertTrue(
            Coordinator(self.db, task, attempt, launcher=self.launch_pr_stage, poll_seconds=0.01).run()["ok"]
        )
        self.assertEqual(self.launches["implement"], 1)
        self.assertEqual(self.launches["reviewer-prompt.md"], 1)
        self.assertEqual(self.launches["address"], 2)

    def finish_review_question(self):
        driver = self.start(["claude"])
        self.with_finding = self.needs_decision = True
        recorded = driver.run()
        with fixture_database.connect(self.db) as conn:
            conn.execute(
                "UPDATE github_actions SET publish_status='published' WHERE result_id=?", (recorded["result_id"],)
            )
        self.cycle([])
        with fixture_database.connect(self.db) as conn:
            item = conn.execute("SELECT work_item_id,version FROM work_items").fetchone()
            question = conn.execute(
                "SELECT event_id,metadata_json FROM work_item_events WHERE event_type='operator_question'"
            ).fetchone()
            lifecycle = conn.execute("SELECT lifecycle FROM tasks WHERE task_id=?", (driver.task_id,)).fetchone()[0]
        self.assertEqual(lifecycle, "waiting_for_user")
        self.assertEqual(json.loads(question[1])["pre_pr_review"]["round_id"], driver.round_id)
        context = commands.CommandContext(
            "operator", "owner", frozenset({"repo:example/backend"}), frozenset({"codex", "claude"})
        )
        return driver, item, question, context

    def test_authenticated_reply_reuses_review_and_resumes_only_address(self):
        driver, item, question, context = self.finish_review_question()
        args = dict(
            context=context,
            command="reply",
            expected_version=item[1],
            expected_event_id=question[0],
            idempotency_key="review-reply",
            body="Use the expected value within the original scope.",
        )
        accepted = commands.execute_command(self.db, item[0], **args)
        replayed = commands.execute_command(self.db, item[0], **args)
        self.assertEqual(accepted, replayed)
        self.needs_decision = False
        with patch(
            "robert_agent.work.operator_queries.source_read",
            return_value=(True, {"state": "open", "title": "Update example", "body": "Update the example"}),
        ):
            self.cycle([])
        task, attempt = self.current()
        self.assertNotEqual(task, driver.task_id)
        with fixture_database.connect(self.db) as conn:
            resumed = store.session(conn, task_id=task)
        self.assertIsNotNone(resumed)
        self.assertEqual(resumed["round_id"], driver.round_id)
        checkpoint = Path(resumed["request"]["operator_reply"]["decision"]["dispositions_path"])
        original = checkpoint.read_bytes()
        checkpoint.write_text("changed after the reply")
        with self.assertRaisesRegex(StageStopped, "checkpoint changed"):
            Coordinator(self.db, task, attempt, launcher=self.launch_pr_stage, poll_seconds=0.01)
        checkpoint.write_bytes(original)
        original_context = validation.context

        def changed_scope(*args, **kwargs):
            settings, rejected = original_context(*args, **kwargs)
            return {**settings, "action_scope": {**settings["action_scope"], "base_branch": "other-base"}}, rejected

        with patch("robert_agent.workflows.pre_pr_review.coordinator.validation.context", side_effect=changed_scope):
            with self.assertRaisesRegex(StageStopped, "original review scope"):
                Coordinator(self.db, task, attempt, launcher=self.launch_pr_stage, poll_seconds=0.01)
        restarted = Coordinator(self.db, task, attempt, launcher=self.launch_pr_stage, poll_seconds=0.01)
        result = restarted.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches["implement"], 1)
        self.assertEqual(self.launches["reviewer-prompt.md"], 1)
        self.assertEqual(self.launches["address"], 2)

    def test_review_reply_rejects_changed_checkpoint_and_worker_override(self):
        driver, item, question, context = self.finish_review_question()
        args = dict(
            context=context,
            command="reply",
            expected_version=item[1],
            expected_event_id=question[0],
            body="Continue in scope.",
        )
        with self.assertRaises(commands.WorkItemConflictError):
            commands.execute_command(
                self.db, item[0], **{**args, "expected_event_id": "stale-question"}, idempotency_key="stale-question"
            )
        with self.assertRaises(commands.WorkItemValidationError):
            commands.execute_command(
                self.db, item[0], **args, idempotency_key="override", routing_mode="manual", requested_worker="codex"
            )
        with fixture_database.connect(self.db) as conn:
            saved = store.session(conn, task_id=driver.task_id)
            Path(saved["report_path"]).write_text("changed report")
        with self.assertRaisesRegex(commands.WorkItemConflictError, "checkpoint changed"):
            commands.execute_command(self.db, item[0], **args, idempotency_key="stale-checkpoint")
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)

    def test_canceled_review_question_cannot_resume(self):
        driver, item, question, context = self.finish_review_question()
        canceled = commands.execute_command(
            self.db,
            item[0],
            context=context,
            command="cancel",
            expected_version=item[1],
            idempotency_key="cancel-review",
        )
        with self.assertRaises(commands.WorkItemConflictError):
            commands.execute_command(
                self.db,
                item[0],
                context=context,
                command="reply",
                expected_version=canceled["item"]["version"],
                expected_event_id=question[0],
                idempotency_key="reply-after-cancel",
                body="Continue in scope.",
            )
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
            self.assertNotIn("operator_reply", store.session(conn, task_id=driver.task_id)["request"])

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

    def test_changed_final_sha_requires_a_bound_reviewer_proof(self):
        driver = self.start(["claude"])
        self.with_finding = True
        result = driver.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches["verify_final"], 1)
        with fixture_database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
            proof = entry["proof"]["final_verification"]
            self.assertEqual(proof["final_sha"], entry["proof"]["head_sha"])
            self.assertEqual(proof["round_id"], driver.round_id)
            self.assertTrue(store.matches_submission(entry, driver.attempt_id, entry["proof"]["fingerprint"]))
            entry["proof"]["final_verification"]["final_sha"] = entry["proof"]["candidate_sha"]
            self.assertFalse(store.matches_submission(entry, driver.attempt_id, entry["proof"]["fingerprint"]))

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

    def test_final_reviewer_schema_upgrade_preserves_old_runs_and_constraints(self):
        import sqlite3
        from robert_agent.persistence.schema import database

        driver = self.start(["claude"])
        with fixture_database.connect(self.db) as conn:
            run = store.new_run(conn, driver.round_id, "review", "claude", driver.attempt_id, self.root, "old-prompt")
            store.update_run(
                conn, run["run_id"], status="running", slot_reserved=1, dispatch_json=json.dumps({"pid": 1073741824})
            )
            old = dict(conn.execute("SELECT * FROM pre_pr_review_runs").fetchone())
            versions = conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='pre_pr_review_runs'").fetchone()[0]
            conn.execute("ALTER TABLE pre_pr_review_runs RENAME TO legacy_runs")
            conn.execute(ddl.replace(",'verify_final'", ""))
            columns = ",".join(old)
            conn.execute(f"INSERT INTO pre_pr_review_runs ({columns}) SELECT {columns} FROM legacy_runs")
            conn.execute("DROP TABLE legacy_runs")
        database.init_database(self.db)
        database.init_database(self.db)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(dict(conn.execute("SELECT * FROM pre_pr_review_runs").fetchone()), old)
            self.assertEqual(conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(), versions)
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='pre_pr_review_runs'").fetchone()[0]
            self.assertIn("verify_final", ddl)
            self.assertIn("UNIQUE (round_id, stage, worker_id, attempt_no)", ddl)
            self.assertIn("UNIQUE (output_dir)", ddl)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertIsNotNone(
                conn.execute("SELECT 1 FROM sqlite_master WHERE name='pre_pr_review_runs_reserved'").fetchone()
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE pre_pr_review_runs SET stage='unsupported'")
            store.new_run(conn, driver.round_id, "verify_final", "claude", driver.attempt_id, self.root, "new-prompt")

    def emulate_pre_upgrade_ready_result(self, driver):
        with fixture_database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
            proof = {key: value for key, value in entry["proof"].items() if key != "final_verification"}
            store.update(conn, driver.round_id, proof_json=json.dumps(proof))
            conn.execute("DELETE FROM pre_pr_review_runs WHERE stage='verify_final'")
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='pre_pr_review_runs'").fetchone()[0]
            conn.execute("ALTER TABLE pre_pr_review_runs RENAME TO legacy_runs")
            conn.execute(ddl.replace(",'verify_final'", ""))
            conn.execute("INSERT INTO pre_pr_review_runs SELECT * FROM legacy_runs")
            conn.execute("DROP TABLE legacy_runs")
        return proof

    def test_upgrade_preserves_old_accepted_frozen_publication(self):
        from robert_agent.delivery.application import guards
        from robert_agent.persistence.schema import database

        driver = self.start(["claude"])
        self.with_finding = True
        self.assertTrue(driver.run()["ok"])
        proof = self.emulate_pre_upgrade_ready_result(driver)
        with patch.object(store, "_matches_final_verification", return_value=True):
            self.cycle([])
        database.init_database(self.db)
        with fixture_database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
            self.assertEqual(entry["proof"], proof)
            actions = queries.ready_actions(conn, 10)
            self.assertEqual(len(actions), 2)
            for action in actions:
                self.assertIsNone(guards.publication_scope_guard(conn, action))

    def test_upgrade_binds_only_the_preexisting_unaudited_result(self):
        from robert_agent.persistence.schema import database

        driver = self.start(["claude"])
        self.with_finding = True
        result = driver.run()
        proof = self.emulate_pre_upgrade_ready_result(driver)
        database.init_database(self.db)
        self.cycle([])
        with fixture_database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
            self.assertEqual(entry["proof"], proof)
            self.assertEqual(len(queries.ready_actions(conn, 10)), 2)
            self.assertTrue(
                store.matches_submission(entry, driver.attempt_id, proof["fingerprint"], result_id=result["result_id"])
            )
            self.assertFalse(store.matches_submission(entry, driver.attempt_id, proof["fingerprint"]))
            self.assertFalse(
                store.matches_submission(entry, driver.attempt_id, proof["fingerprint"], result_id="new-result")
            )
            entry["proof"]["head_sha"] = proof["candidate_sha"]
            self.assertFalse(
                store.matches_submission(entry, driver.attempt_id, proof["fingerprint"], result_id=result["result_id"])
            )

    def test_final_verifier_reuses_the_finished_address_worktree(self):
        driver = self.start(["claude"])
        self.with_finding = True
        self.assertTrue(driver.run()["ok"])
        with fixture_database.connect(self.db) as conn:
            address = next(run for run in store.runs(conn, driver.round_id) if run["stage"] == "address_findings")
            self.assertEqual(address["slot_reserved"], 0)
            self.assertEqual(address["status"], "succeeded")
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
