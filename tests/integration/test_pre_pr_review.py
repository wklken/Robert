from collections import Counter
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
from tests.support import database as fixture_database
from tests.support.repository_cycle import _new_pr_actions, _verification_evidence
from tests.support.review_coordinator import ReviewCoordinatorFixture


class PrePublicationReviewTests(ReviewCoordinatorFixture):
    def start(self, workers):
        config = yaml.safe_load(self.config_path.read_text())
        config["repos"][0]["routes"] = {"new-pr": {"pre_pr_review": {"workers": workers}}}
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle(
            [
                {
                    "id": "implement",
                    "number": 42,
                    "source_type": "issue",
                    "event_type": "assigned",
                    "actor_login": "maintainer",
                    "assigned_to": "robert-bot",
                    "assignment_actor_login": "maintainer",
                    "body": "Update the example",
                    "intent": "small_change",
                    "title": "Update example",
                    "state": "open",
                    "html_url": "https://github.com/example/backend/issues/42",
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
            "consumed_event_fingerprints": [saved["request"]["event"]["event_fingerprint"]],
            "handoff": "Update example and verify the result.",
        }
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
        self.launches = Counter()
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
        elif prompt.name.startswith("address-"):
            self.launches["address"] += 1
            if not self.fail_disposition:
                report_sha = prompt.stem.removeprefix("address-")
                candidate_sha = git(tree, "rev-parse", "HEAD")
                decisions = []
                if self.with_finding:
                    (tree / "source.py").write_text("value = 4\n")
                    git(tree, "commit", "-am", "address finding")
                    decisions = [
                        {"id": "F1", "status": "fixed", "reason": "Updated the value and verified its consumer."}
                    ]
                (output / "dispositions.json").write_text(
                    json.dumps(
                        {
                            "candidate_sha": candidate_sha,
                            "final_sha": git(tree, "rev-parse", "HEAD"),
                            "report_sha256": report_sha,
                            "dispositions": decisions,
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

    def test_single_reviewer_and_processing_gate_result(self):
        driver = self.start(["claude"])
        early = submission.record_result(self.db, driver.envelope(self.submission))
        self.assertEqual(early["reason_code"], "pre_pr_review_required")
        result = driver.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches, {"implement": 1, "prepare-prompt.md": 1, "reviewer-prompt.md": 1, "address": 1})
        with fixture_database.connect(self.db) as conn:
            saved = store.session(conn, task_id=driver.task_id)
            self.assertEqual(saved["final_result_id"], result["result_id"])
            self.assertEqual(saved["proof"]["head_sha"], saved["manifest"]["head_sha"])
            (body,) = conn.execute("SELECT metadata_json FROM github_actions WHERE action_type='open_pr'").fetchone()
            self.assertIn("未处理及原因", json.loads(body)["body"])
            self.assertEqual(queries.ready_actions(conn, 10), [])  # Pending normal audit.

    def test_failed_synthesis_reuses_both_reviewers(self):
        driver = self.start(["codex", "claude"])
        self.fail_synthesis = True
        with patch.object(driver.stopping, "wait", side_effect=InterruptedError), self.assertRaises(InterruptedError):
            driver.run()
        restarted = Coordinator(
            self.db, driver.task_id, driver.attempt_id, launcher=self.launch_pr_stage, poll_seconds=0.01
        )
        result = restarted.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            self.launches,
            {
                "implement": 1,
                "prepare-prompt.md": 1,
                "reviewer-prompt.md": 2,
                "synthesis": 2,
                "address": 1,
            },
        )
        with fixture_database.connect(self.db) as conn:
            syntheses = [run for run in store.runs(conn, driver.round_id) if run["stage"] == "synthesis"]
        self.assertEqual([run["status"] for run in syntheses], ["failed", "succeeded"])
        self.assertEqual(syntheses[0]["prompt_sha256"], syntheses[1]["prompt_sha256"])

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
