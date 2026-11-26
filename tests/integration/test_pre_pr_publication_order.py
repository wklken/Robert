import json
from datetime import datetime, timezone
from pathlib import Path

from robert_agent.adapters.git.review_revision import git
from robert_agent.context import project
from robert_agent.delivery import queries
from robert_agent.delivery.application import audit
from robert_agent.execution.stages import StageStopped
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.supervision.application import observation
from tests.support import database
from tests.support.pre_pr_publication import PrePRPublicationFixture


class EarlyReviewPublicationTests(PrePRPublicationFixture):
    def test_review_starts_only_after_draft_pr_is_confirmed(self):
        driver, remote = self.setup_publication()
        result = driver.run()

        self.assertTrue(result["ok"], result)
        self.assertEqual(remote.events, ["push", "open", "review", "report", "description", "completion"])
        self.assertIn("Fixes #42", remote.pr["body"])
        self.cycle([])
        with database.connect(self.db) as conn:
            entry = store.session(conn, round_id=driver.round_id)
            self.assertEqual(entry["proof"]["publication"]["initial"]["number"], 72)
            self.assertEqual(entry["proof"]["head_sha"], remote.head)
            self.assertEqual(queries.ready_actions(conn, 10), [])
            self.assertEqual(
                conn.execute("SELECT lifecycle FROM tasks WHERE task_id = ?", (driver.task_id,)).fetchone()[0],
                "completed",
            )
            rows = conn.execute(
                "SELECT a.action_type, a.metadata_json, i.status, i.remote_evidence_json "
                "FROM github_actions a LEFT JOIN publication_intents i ON i.action_id = a.action_id "
                "WHERE a.task_id = ?",
                (driver.task_id,),
            ).fetchall()
        self.assertEqual({row[0] for row in rows}, {"open_pr", "push_existing_pr"})
        for action_type, metadata, status, evidence in rows:
            self.assertEqual(status, "published", action_type)
            self.assertEqual(json.loads(metadata)["publish"]["receipt"], json.loads(evidence)["receipt"])
            self.assertEqual(json.loads(evidence)["receipt"]["result_head_sha"], remote.head)
        observed = observation.collect(self.db, now=datetime.now(timezone.utc))["observations"]
        delivery = [
            item
            for item in observed
            if item["scope_id"] == driver.task_id and item["rule_id"] == "delivery_inconsistent"
        ]
        self.assertEqual([item["classification"] for item in delivery], ["healthy"])
        with database.connect(self.db) as conn:
            result_id = store.session(conn, task_id=driver.task_id)["final_result_id"]
            source = project.memory_provenance(conn, "repo:example/backend", driver.task_id, result_id)
        self.assertEqual(source["stage"], "publication_confirmed")

    def test_web_origin_comments_are_scoped_to_the_registered_pull_request(self):
        driver, remote = self.setup_publication(local=True)
        result = driver.run()

        self.assertTrue(result["ok"], result)
        self.assertEqual([comment["id"] for comment in remote.comments], [1, 2])
        with database.connect(self.db) as conn:
            publication = store.session(conn, round_id=driver.round_id)["request"]["review_publication"]
        self.assertEqual(publication["report"]["target_url"], remote.url)
        self.assertEqual(publication["completion"]["target_url"], remote.url)
        wrong_target = {**publication["pr"], "url": f"https://github.com/{remote.repo}/pull/99"}
        self.assertIsNotNone(
            audit.managed_review_comment_violation(
                "report", wrong_target, driver.scope, ["open_pr", "push_existing_pr"]
            )
        )

    def test_description_edit_after_draft_registration_is_preserved(self):
        driver, remote = self.setup_publication()
        launch = driver.launcher

        def edit_metadata(**kwargs):
            process = launch(**kwargs)
            if Path(kwargs["prompt_path"]).name == "reviewer-prompt.md":
                remote.pr["body"] = "Maintainer update"
            return process

        driver.launcher = edit_metadata
        result = driver.run()

        self.assertTrue(result["ok"], result)
        self.assertEqual(remote.pr["body"], "Maintainer update")
        self.assertEqual(remote.events, ["push", "open", "review", "report", "completion"])

    def test_changed_remote_base_stops_completion_even_when_no_push_is_needed(self):
        driver, remote = self.setup_publication()
        completed = remote.completed

        def advance_base(args, value, *, effect=None):
            result = completed(args, value, effect=effect)
            if effect == "report":
                remote.base = "f" * 40
            return result

        remote.completed = advance_base

        with self.assertRaisesRegex(StageStopped, "review publication base changed"):
            driver.run()
        self.assertEqual(remote.events, ["push", "open", "review", "report"])

    def test_repaired_commit_updates_the_same_draft(self):
        driver, remote = self.setup_publication(("claude", "codex"))
        self.with_finding = True
        result = driver.run()

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            remote.events,
            ["push", "open", "review", "review", "report", "repair", "push", "description", "completion"],
        )
        with database.connect(self.db) as conn:
            state = store.session(conn, round_id=driver.round_id)["request"]["review_publication"]
        self.assertNotEqual(state["pr"]["initial_head"], remote.head)
        self.assertEqual(state["complete"]["head_sha"], remote.head)
        self.assertIn("Updated the value", remote.comments[-1]["body"])

    def test_required_gate_retry_updates_the_existing_draft_without_another_review(self):
        driver, remote = self.setup_publication()
        self.with_finding = True
        launch = driver.launcher
        failed_head = []

        def fail_first_gate(**kwargs):
            prompt = Path(kwargs["prompt_path"])
            addressing = prompt.name.startswith("address-")
            if addressing and failed_head:
                self.assertIn("verification-retry", prompt.read_text())
                self.assertEqual(git(kwargs["worktree_path"], "rev-parse", "HEAD"), failed_head[0])
            process = launch(**kwargs)
            if addressing and not failed_head:
                output = Path(kwargs["log_dir"]) / "dispositions.json"
                result = json.loads(output.read_text())
                failed_head.append(result["final_sha"])
                result["verification"][0].update(status="failed", exit_code=1)
                output.write_text(json.dumps(result))
            return process

        driver.launcher = fail_first_gate
        self.assertTrue(driver.run()["ok"])
        self.assertEqual(self.launches["reviewer-prompt.md"], 1)
        self.assertEqual(self.launches["address"], 2)
        self.assertEqual(self.launches["verify_final"], 0)
        self.assertEqual(remote.events.count("open"), 1)
        self.assertEqual(remote.events.count("report"), 1)
        self.assertEqual(remote.events.count("completion"), 1)
        self.assertEqual(remote.head, failed_head[0])

    def test_remote_head_drift_stops_before_repair(self):
        driver, remote = self.setup_publication()
        self.with_finding = True
        launch = driver.launcher

        def advance_remote(**kwargs):
            result = launch(**kwargs)
            if Path(kwargs["prompt_path"]).name == "reviewer-prompt.md":
                remote.head = remote.pr["head"]["sha"] = "a" * 40
            return result

        driver.launcher = advance_remote
        with self.assertRaisesRegex(StageStopped, "identity or head changed"):
            driver.run()
        self.assertEqual(remote.events, ["push", "open", "review"])

    def test_high_finding_requests_one_bound_final_review(self):
        driver, remote = self.setup_publication()
        self.with_finding = True
        launch = driver.launcher

        def high_finding(**kwargs):
            value = launch(**kwargs)
            path = Path(kwargs["log_dir"]) / "result.json"
            if Path(kwargs["prompt_path"]).name == "reviewer-prompt.md":
                data = json.loads(path.read_text())
                data["findings"][0]["severity"] = "high"
                path.write_text(json.dumps(data))
            return value

        driver.launcher = high_finding

        result = driver.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches["verify_final"], 1)
        self.assertIn("critical_or_high_finding", remote.comments[-1]["body"])

    def test_unreviewed_changed_file_gets_targeted_check_without_repair(self):
        driver, remote = self.setup_publication(("claude", "codex"))
        launch = driver.launcher

        def report_gap(**kwargs):
            result = launch(**kwargs)
            if Path(kwargs["prompt_path"]).name == "reviewer-prompt.md":
                path = Path(kwargs["log_dir"]) / "result.json"
                value = json.loads(path.read_text())
                value["coverage"].update(reviewed_paths=[], unreviewed_paths=["source.py"])
                path.write_text(json.dumps(value))
            return result

        driver.launcher = report_gap
        result = driver.run()

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches["address"], 0)
        self.assertEqual(self.launches["verify_final"], 1)
        self.assertIn("unreviewed_changed_path: source.py", remote.comments[-1]["body"])

    def test_github_decision_delivers_the_question_to_the_original_request(self):
        driver, remote = self.setup_publication()
        self.with_finding = self.needs_decision = True
        self.assertTrue(driver.run()["ok"])

        with database.connect(self.db) as conn:
            [(kind, target, metadata)] = conn.execute(
                "SELECT action_type, target_url, metadata_json FROM github_actions"
            ).fetchall()
            self.assertEqual((kind, target), ("comment", "https://github.com/example/backend/issues/42"))
            self.assertTrue(json.loads(metadata)["body"].startswith("@maintainer"))
            self.assertIn("Robert 工作台", json.loads(metadata)["body"])
            self.assertEqual(store.session(conn, round_id=driver.round_id)["status"], "waiting_for_user")
        self.assertEqual(remote.events.count("push"), 1)
        self.assertIn("待决定", remote.comments[-1]["body"])

    def test_revoked_source_trust_prevents_publication_after_review(self):
        import yaml

        driver, remote = self.setup_publication()
        launch = driver.launcher

        def revoke(**kwargs):
            result = launch(**kwargs)
            if Path(kwargs["prompt_path"]).name == "reviewer-prompt.md":
                settings = yaml.safe_load(self.config_path.read_text())
                settings["repos"][0]["trusted_actors"] = ["other-maintainer"]
                self.config_path.write_text(yaml.safe_dump(settings))
            return result

        driver.launcher = revoke
        with self.assertRaisesRegex(StageStopped, "does not authorize"):
            driver.run()
        self.assertEqual(remote.events, ["push", "open", "review"])
