"""Deploy the final revision in the managed draft and gate its final publication."""

import json
from pathlib import Path
from unittest.mock import patch

import yaml

from robert_agent.adapters.git.review_revision import git
from robert_agent.delivery.application.review_publication import Publication, TransientPublicationError
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.workflows.pre_pr_review.deployment_validation import DeploymentValidationBlocked
from tests.support import database
from tests.support.pre_pr_publication import PrePRPublicationFixture


class DeploymentValidationTests(PrePRPublicationFixture):
    def start_validation(self, *, status="passed"):
        skill = self.root / "SKILL.md"
        skill.write_text("---\nname: fast-local-k8s-context\n---\n")
        driver, remote = self.setup_publication(deployment_validation={"worker": "codex", "skill_path": str(skill)})
        launch = driver.launcher
        self.checked = []

        def validate_deployment(**kwargs):
            prompt = Path(kwargs["prompt_path"])
            if not prompt.name.startswith("validate-deployment-"):
                return launch(**kwargs)
            self.assertEqual(kwargs["worker_agent_config"]["name"], "codex")
            self.assertTrue(remote.pr["draft"])
            self.assertEqual(remote.events[-1], "repair" if self.with_finding else "report")
            text = prompt.read_text()
            self.assertIn(str(skill), text)
            self.assertIn("dev/bk-e2e", text)
            result = json.loads(text.split("Example:\n\n```json\n", 1)[1].split("\n```", 1)[0])
            tree = Path(kwargs["worktree_path"])
            self.assertEqual(git(tree, "rev-parse", "HEAD"), result["head_sha"])
            self.checked.append((remote.head, result["head_sha"]))
            remote.events.append("deploy")
            result.update(status=status, summary="已检查 API 返回与恢复结果。")
            if status == "passed":
                result.update(
                    images={"dashboard": "sha256:" + "a" * 64},
                    assertions=[{"action": "GET API", "expected": "200", "observed": "200", "passed": True}],
                    cleanup="verified",
                )
            (Path(kwargs["log_dir"]) / "deployment-validation.json").write_text(json.dumps(result))
            return {"ok": True, "process_exited": True, "returncode": 0, "pid": 1073741824}

        driver.launcher = validate_deployment
        return driver, remote

    def test_review_repair_deploys_final_sha_before_final_push(self):
        driver, remote = self.start_validation()
        self.with_finding = True
        self.assertTrue(driver.run()["ok"])
        self.assertEqual(len(self.checked), 1)
        candidate, final = self.checked[0]
        self.assertNotEqual(candidate, final)
        self.assertEqual(final, remote.head)
        self.assertEqual(
            remote.events, ["push", "open", "review", "report", "repair", "deploy", "push", "description", "completion"]
        )
        self.assertIn("Minikube 验证通过", remote.comments[-1]["body"])
        self.assertIn(final, remote.comments[-1]["body"])
        with database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
        self.assertEqual(entry["proof"]["deployment_validation"]["head_sha"], final)
        self.assertTrue(store.matches_review_completion(entry, entry["proof"]))
        prompt = Path(driver.manifest["artifact_root"]) / f"validate-deployment-{final}.md"
        self.assertIn(f"git diff {driver.manifest['merge_base_sha']} {final}", prompt.read_text())

    def test_blocked_deployment_leaves_review_draft(self):
        self.assert_draft_blocked("blocked", "deployment blocked")

    def assert_draft_blocked(self, status, message):
        driver, remote = self.start_validation(status=status)
        with self.assertRaisesRegex(DeploymentValidationBlocked, message):
            driver.run()
        self.assertEqual(len(self.checked), 1)
        self.assertEqual(remote.head, self.checked[0][0])
        self.assertEqual(remote.events.count("push"), 1)
        self.assertTrue(remote.pr["draft"])
        self.assertIn("第一轮评审", remote.comments[0]["body"])
        self.assertIn("Minikube 验证待处理", remote.comments[1]["body"])
        with database.connect(self.db) as conn:
            entry = store.session(conn, task_id=driver.task_id)
        self.assertEqual(entry["status"], "attention")
        self.assertFalse(entry["proof"])

    def test_runtime_code_cannot_skip(self):
        self.assert_draft_blocked("skipped", "only Markdown")

    def test_lost_status_comment_does_not_retry_an_incomplete_deployment(self):
        driver, remote = self.start_validation(status="blocked")
        with (
            patch.object(
                Publication, "deployment_blocked", side_effect=TransientPublicationError("GitHub unavailable")
            ),
            self.assertRaisesRegex(DeploymentValidationBlocked, "deployment blocked"),
        ):
            driver.run()
        self.assertEqual(len(self.checked), 1)
        self.assertEqual(remote.events.count("push"), 1)

    def test_configuration_revoked_while_worker_queues_refuses_launch(self):
        driver, remote = self.start_validation()
        original = driver.launch_with_slot

        def revoke(run, *args):
            if run["stage"] == "validate_deployment":
                config = yaml.safe_load(self.config_path.read_text())
                del config["repos"][0]["routes"]["new-pr"]["pre_pr_review"]["deployment_validation"]
                self.config_path.write_text(yaml.safe_dump(config))
            return original(run, *args)

        with (
            patch.object(driver, "launch_with_slot", side_effect=revoke),
            self.assertRaisesRegex(DeploymentValidationBlocked, "does not authorize"),
        ):
            driver.run()
        self.assertFalse(self.checked)
        self.assertEqual(remote.events.count("push"), 1)
        with database.connect(self.db) as conn:
            runs = store.runs(conn, driver.round_id)
        deployment = next(run for run in runs if run["stage"] == "validate_deployment")
        self.assertFalse(deployment["slot_reserved"])
        self.assertIsNone(deployment["dispatch"].get("pid"))
