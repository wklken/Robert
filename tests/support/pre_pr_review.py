from collections import Counter
import json
from pathlib import Path

import yaml

from robert_agent.adapters.git.review_revision import git
from robert_agent.configuration import validation as config_validation
from robert_agent.delivery import queries
from robert_agent.work.application import commands, tapd_scan
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.workflows.pre_pr_review.coordinator import Coordinator
from tests.support.work_items import example_choices
from tests.support import database as fixture_database
from tests.support.repository_cycle import _new_pr_actions, _verification_evidence
from tests.support.review_coordinator import ReviewCoordinatorFixture
from tests.support.tapd import FakeClient, comment
from tests.support.tapd_cycle import TAPD_CONFIG


class PrePRReviewFixture(ReviewCoordinatorFixture):
    def start(
        self,
        workers,
        *,
        local=False,
        tapd=False,
        github_comment=False,
        legacy=True,
        legacy_report=True,
        pr_first=False,
        deployment_validation=None,
    ):
        config = yaml.safe_load(self.config_path.read_text())
        review = {"workers": workers}
        if deployment_validation is not None:
            review["deployment_validation"] = deployment_validation
        config["repos"][0]["routes"] = {"new-pr": {"pre_pr_review": review}}
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
            plan = saved["request"]["plan"]
            if legacy:
                plan.pop("review_policy", None)
            if legacy_report:
                plan.pop("review_report_mode", None)
            if not pr_first:
                plan.pop("review_publication", None)
            if legacy or legacy_report or not pr_first:
                store.update(conn, saved["round_id"], request_json=json.dumps(saved["request"], sort_keys=True))
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
        self.legacy_report = legacy_report
        self.judgment_workers = set()
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
            if value["final_sha"] != value["candidate_sha"]:
                self.assertIn("value = 4", (tree / "source.py").read_text())
                self.assertIn("value = 3", Path(value["additional_diff_path"]).read_text())
            else:
                self.assertEqual(Path(value["additional_diff_path"]).read_text(), "")
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
                finding_id = "F1"
                if not self.legacy_report and self.with_finding:
                    example = prompt.read_text().split("Example:\n\n```json\n", 1)[1].split("\n```", 1)[0]
                    finding_id = json.loads(example)["dispositions"][0]["id"]
                if self.with_finding:
                    if (tree / "source.py").read_text() != "value = 4\n":
                        (tree / "source.py").write_text("value = 4\n")
                        git(tree, "commit", "-am", "address finding")
                    decisions = [
                        {"id": finding_id, "status": "fixed", "reason": "Updated the value and verified its consumer."}
                    ]
                if self.needs_decision:
                    decisions = [
                        {
                            "id": finding_id,
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
            if (self.with_finding or not self.legacy_report) and prompt.name == "reviewer-prompt.md":
                path = output / "result.json"
                value = json.loads(path.read_text())
                if not self.legacy_report:
                    value["requires_judgment"] = kwargs["worker_agent_config"]["name"] in self.judgment_workers
                findings = [
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
                if self.with_finding:
                    value["findings"] = findings
                path.write_text(json.dumps(value))
            return launched
        return {"ok": True, "process_exited": True, "returncode": 0, "pid": 1073741824}

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
