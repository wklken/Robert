import json
from pathlib import Path
from unittest.mock import patch

from robert_agent.delivery.application import validation
from robert_agent.execution import capacity
from robert_agent.execution.stages import StageStopped
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.work.application import commands
from robert_agent.workflows.pre_pr_review.coordinator import Coordinator
from tests.support import database as fixture_database
from tests.support.pre_pr_review import PrePRReviewFixture
from tests.support.tapd import STORY
from tests.support.work_items import example_choices


class LegacyReviewDecisionResumeTests(PrePRReviewFixture):
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
