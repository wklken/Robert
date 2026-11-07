from tests.support import database as fixture_database
from collections import Counter
import json
from pathlib import Path
from types import SimpleNamespace

import yaml
from robert_agent.persistence.rounds import vote as vote_store
from robert_agent.workflows.vote import publish as vote_publish
from tests.support.vote_coordinator import VoteCoordinatorFixture


class VoteCoordinatorTests(VoteCoordinatorFixture):
    def test_rate_limit_automatically_switches_only_affected_voter(self):
        config = yaml.safe_load(self.fixture.config_path.read_text())
        config["workers"]["claude"].update(model="primary-model", fallback_model="fallback-model")
        self.fixture.config_path.write_text(yaml.safe_dump(config))
        driver = self.start()
        original = driver.launcher
        models = []

        def limited(**kwargs):
            if kwargs["worker_agent_config"]["name"] == "claude":
                models.append(kwargs["model"])
                if kwargs["model"] == "primary-model":
                    log = Path(kwargs["log_dir"]) / "stdout.log"
                    log.write_text(json.dumps({"type": "turn.failed", "error": {"message": "429 Too Many Requests"}}))
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
        self.assertTrue(report.is_file())
        self.assertEqual(models, ["primary-model", "fallback-model"])
        self.assertEqual(sum(call[0:2] == ("vote", "codex") for call in self.calls), 1)

    def test_issue_file_handoff_preserves_original_votes_and_no_worktree(self):
        driver = self.start()
        report = driver.run().read_text()
        self.assertIn("reason from codex", report)
        self.assertIn("reason from claude", report)
        self.assertIn("平票", report)
        self.assertIn("主 Agent 建议", report)
        self.assertIsNone(driver.manifest["source_worktree"])
        self.assertEqual(len(set(c[2] for c in self.calls if c[0] == "vote")), 1)
        self.fixture.cycle([])
        with fixture_database.connect(self.db) as conn:
            row = conn.execute("SELECT audit_status,target_url FROM github_actions").fetchone()
            self.assertEqual(tuple(row), ("accepted", "https://github.com/example/backend/issues/42"))

    def test_partial_failure_only_retries_missing_model_and_keeps_prompt(self):
        driver = self.start()
        self.fail_worker = "claude"
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            driver.run()
        self.fail_worker = None
        self.driver().run()
        counts = Counter((c[0], c[1]) for c in self.calls)
        self.assertEqual(counts[("prepare", "codex")], 1)
        self.assertEqual(counts[("vote", "codex")], 1)
        self.assertEqual(counts[("vote", "claude")], 2)
        self.assertEqual(len(set(c[2] for c in self.calls if c[0] == "vote")), 1)

    def test_new_requests_queue_as_separate_rounds_and_duplicate_events_do_not_repeat(self):
        first = self.start()
        self.fixture.cycle([self.event("second"), self.event("second"), self.event("third")])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM vote_sessions").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
        first.run()
        self.fixture.finish_task(first.task_id)
        self.fixture.cycle([])
        second = self.driver()
        self.assertNotEqual(first.round_id, second.round_id)
        second.run()

    def test_pr_uses_pinned_code_and_posts_to_pr_conversation(self):
        self.issue = False
        driver = self.start()
        report = driver.run()
        self.assertEqual(driver.manifest["head_sha"], self.fixture.git_fixture.head)
        self.assertIsNotNone(driver.manifest["source_worktree"])
        self.assertTrue(report.is_file())
        self.fixture.cycle([])
        with fixture_database.connect(self.db) as conn:
            row = conn.execute("SELECT action_type,target_url,audit_status FROM github_actions").fetchone()
            self.assertEqual(tuple(row), ("comment", "https://github.com/example/backend/pull/42", "accepted"))

    def test_report_runs_through_audit_publish_and_completion(self):
        driver = self.start()
        driver.run()
        self.fixture.cycle([])
        self.assertEqual(len(self.publish_all()), 1)
        self.fixture.cycle([], skip_publish=False)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(vote_store.session(conn, task_id=driver.task_id)["status"], "published")
            self.assertEqual(
                conn.execute("SELECT lifecycle FROM tasks WHERE task_id=?", (driver.task_id,)).fetchone()[0],
                "completed",
            )

    def test_clarification_does_not_vote_and_reply_continues_same_round(self):
        self.clarify = True
        driver = self.start()
        self.assertIsNone(driver.run())
        self.assertFalse(any(c[0] == "vote" for c in self.calls))
        # Publish/accept the question through the existing waiting-result protocol.
        self.fixture.cycle([])
        self.publish_all()
        self.fixture.cycle([], skip_publish=False)
        self.clarify = False
        reply = {**self.event("reply"), "body": "@robert-bot latency target is 1 second", "intent": "analysis"}
        self.fixture.cycle([reply])
        next_driver = self.driver()
        self.assertEqual(next_driver.round_id, driver.round_id)
        self.assertNotEqual(next_driver.task_id, driver.task_id)
        next_driver.run()
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM vote_sessions").fetchone()[0], 1)

    def test_repeated_or_historical_reply_cannot_answer_a_new_clarification(self):
        self.clarify = True
        driver = self.start()
        driver.run()
        self.fixture.cycle([])
        self.publish_all()
        self.fixture.cycle([], skip_publish=False)
        reply = {**self.event("reply"), "body": "@robert-bot target is one second", "intent": "analysis"}
        self.fixture.cycle([reply])
        self.driver().run()  # Still ambiguous: ask a second question.
        self.fixture.cycle([])
        self.publish_all()
        self.fixture.cycle([], skip_publish=False)
        with fixture_database.connect(self.db) as conn:
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        for event in [reply, {**reply, "id": "historical", "event_at": "2020-01-01T00:00:00Z"}]:
            self.fixture.cycle([event])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], before)
            entry = vote_store.session(conn, round_id=driver.round_id)
            self.assertEqual(len(entry["request"]["event"]["clarifications"]), 1)
            self.assertEqual(entry["status"], "waiting_for_user")

    def test_classification_recommendation_activates_semantic_vote_request(self):
        from robert_agent.delivery.application.submission import record_result

        event = {
            **self.event(),
            "intent": "unclear",
            "body": "@robert-bot 请让三个独立智能体各选一种路线，并报告各自的选择理由",
        }
        self.fixture.cycle([event])
        task, attempt = self.fixture.current()
        result = record_result(
            self.db,
            {
                "task_id": task,
                "attempt_id": attempt,
                "output_type": "classification_result",
                "submission": {
                    "planned_github_actions": [],
                    "consumed_event_fingerprints": ["comment:first"],
                    "verification": [],
                    "used_skills": [],
                    "handoff": "Classified request",
                    "recommended_route": "vote",
                },
            },
        )
        self.assertTrue(result["ok"], result)
        self.fixture.cycle([])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM vote_sessions").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT route_id FROM tasks WHERE parent_task_id=?", (task,)).fetchone()[0], "vote"
            )

    def test_submitted_review_vote_tracks_the_triggering_review_body(self):
        import subprocess
        from unittest.mock import patch
        from robert_agent.workflows.vote import workspace as vote_workspace

        self.issue = False
        event = {**self.event("123"), "event_type": "review"}
        source = self.source()
        review = {"id": 123, "body": event["body"]}
        self.fixture.cycle([event])
        driver = self.driver()
        driver.source_reader = vote_workspace.read_source
        original_run = subprocess.run

        def transport(command, **kwargs):
            if command[0] != "gh":
                return original_run(command, **kwargs)
            value = [[review]] if "/reviews?" in command[2] else [[]]
            return SimpleNamespace(returncode=0, stdout=json.dumps(value))

        with (
            patch("robert_agent.workflows.vote.workspace.api", return_value=source),
            patch("robert_agent.workflows.vote.workspace.subprocess.run", side_effect=transport),
        ):
            driver.prepare()
            self.assertIn("review:123", driver.manifest["sources"])
            review["body"] = "Do not vote now"
            from robert_agent.execution.stages import StageSuperseded

            with self.assertRaises(StageSuperseded):
                driver.check_sources()

    def test_review_trigger_publication_checks_the_review_submission_endpoint(self):
        self.issue = False
        event = {**self.event("123"), "event_type": "review"}
        review = {"id": 123, "kind": "review", "body": event["body"]}
        source = {**self.source(), "discussion": [review]}
        self.fixture.cycle([event])
        driver = self.driver()
        driver.source_reader = lambda *args: source
        driver.run()
        self.fixture.cycle([])
        with fixture_database.connect(self.db) as conn:
            conn.row_factory = __import__("sqlite3").Row
            action = dict(conn.execute("SELECT * FROM github_actions").fetchone())
            scope = json.loads(conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0])["audit"][
                "action_scope"
            ]
        calls = []

        def transport(command, **kwargs):
            calls.append(command)
            if "--method" in command:
                body = next(x[5:] for x in command if x.startswith("body="))
                value = {"id": 77, "body": body, "user": {"login": "robert-bot"}}
            elif "comments?" in command[2]:
                value = [[]]
            elif "/reviews?" in command[2]:
                value = [[review]]
            else:
                value = source
            return SimpleNamespace(returncode=0, stdout=json.dumps(value))

        self.assertTrue(vote_publish.publish(action, scope, transport)["ok"])
        self.assertTrue(any("/pulls/42/reviews?" in c[2] for c in calls))

    def test_issue_can_request_source_before_ballot_freeze(self):
        self.need_code = True
        driver = self.start()
        driver.run()
        self.assertIsNotNone(driver.manifest["source_worktree"])
        self.assertEqual(Counter(c[0] for c in self.calls)["prepare"], 2)

    def test_publication_ambiguous_response_reconciles_without_second_post(self):
        driver, action, scope = self.publication()
        comments = []
        posts = []

        def transport(command, **kwargs):
            endpoint = command[2]
            if "--method" in command:
                posts.append(command)
                comments.append(
                    {"id": 12, "body": json.loads(action["metadata_json"])["body"], "user": {"login": "robert-bot"}}
                )
                return SimpleNamespace(returncode=1, stdout="")
            if "comments?" in endpoint:
                value = [comments]
            else:
                value = self.source()
            return SimpleNamespace(returncode=0, stdout=json.dumps(value))

        first = vote_publish.publish(action, scope, transport)
        self.assertFalse(first["ok"])
        second = vote_publish.publish(action, scope, transport)
        self.assertTrue(second["deduplicated"])
        self.assertEqual(len(posts), 1)
        # Attempt identity cannot create a new publication key for the same round.
        key = vote_publish.publication_keys(action, scope)
        self.assertEqual(key, vote_publish.publication_keys({**action, "action_id": "other"}, scope))

    def test_changed_source_suppresses_publication(self):
        driver, action, scope = self.publication()

        def transport(command, **kwargs):
            if "comments?" in command[2]:
                value = [[]]
            else:
                value = {**self.source(), "body": "changed decision"}
            self.assertNotIn("--method", command)
            return SimpleNamespace(returncode=0, stdout=json.dumps(value))

        self.assertEqual(vote_publish.publish(action, scope, transport)["status"], "superseded")
