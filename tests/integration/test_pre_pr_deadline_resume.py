"""Automatic deadline recovery renews only a granted, stopped checkpoint."""

import json
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import yaml

from robert_agent.adapters.git.review_revision import git
from robert_agent.execution import operations, rounds
from robert_agent.execution.application import evidence, monitor
from robert_agent.execution.stages import StageStopped
from robert_agent.foundation import values
from robert_agent.persistence import execution_control
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.work.application import preparation
from robert_agent.workflows.pre_pr_review.coordinator import Coordinator
from tests.support import database
from tests.support.repository_cycle import _new_pr_actions, _verification_evidence
from tests.support.review_coordinator import ReviewCoordinatorFixture


class PrePRDeadlineResumeTests(ReviewCoordinatorFixture):
    def checkpoint(self):
        config = yaml.safe_load(self.config_path.read_text())
        config["repos"][0]["routes"] = {"new-pr": {"pre_pr_review": {"workers": ["claude"]}}}
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle(
            [
                {
                    "id": "implement",
                    "number": 42,
                    "source_type": "issue",
                    "event_type": "assigned",
                    "actor_login": "maintainer",
                    "assignment_actor_login": "maintainer",
                    "assigned_to": "robert-bot",
                    "body": "Update the example",
                    "intent": "small_change",
                    "title": "Update example",
                    "state": "open",
                    "html_url": "https://github.com/example/backend/issues/42",
                }
            ]
        )
        self.task, self.attempt = self.current()
        with database.connect(self.db) as conn:
            entry = store.session(conn, task_id=self.task)
            entry["request"]["plan"].pop("review_publication", None)
            store.update(conn, entry["round_id"], request_json=json.dumps(entry["request"]))
        self.stage_calls = []
        self.driver = Coordinator(self.db, self.task, self.attempt, launcher=self.launch_stage, poll_seconds=0.01)
        git(
            self.driver.repo["repo_root"],
            "worktree",
            "add",
            "-b",
            self.driver.branch,
            self.driver.assigned_path,
            self.git_fixture.base,
        )
        git(self.driver.assigned_path, "config", "user.name", "Test")
        git(self.driver.assigned_path, "config", "user.email", "test@example.invalid")
        with patch.object(self.driver, "no_repair_disposition", side_effect=StageStopped("checkpoint")):
            with self.assertRaisesRegex(StageStopped, "checkpoint"):
                self.driver.run()
        self.now = store.now()
        self.expired = (values.parse_time(self.now) - timedelta(minutes=1)).isoformat()
        with database.connect(self.db) as conn:
            store.update(
                conn,
                self.driver.round_id,
                status="attention",
                deadline_at=self.expired,
                error="review: pre-publication review deadline expired",
            )
            saved = store.session(conn, task_id=self.task)
            self.request = {**saved["request"], "final_repair_count": 1, "base_refresh_count": 1}
            store.update(conn, self.driver.round_id, request_json=json.dumps(self.request))
            self.original_runs = store.runs(conn, self.driver.round_id)
            metadata = json.loads(
                conn.execute("SELECT metadata_json FROM attempts WHERE attempt_id=?", (self.attempt,)).fetchone()[0]
            )
            metadata["dispatch"] = {"pid": 1073741824, "process_exited": True, "returncode": 0}
            self.workstream_id = conn.execute(
                "SELECT workstream_id FROM tasks WHERE task_id=?", (self.task,)
            ).fetchone()[0]
            conn.execute(
                "UPDATE attempts SET status='running',started_at=?,heartbeat_at=?,metadata_json=? WHERE attempt_id=?",
                (self.now, self.now, json.dumps(metadata), self.attempt),
            )
            conn.execute("UPDATE tasks SET lifecycle='running' WHERE task_id=?", (self.task,))
            self.original_prompt = Path(saved["request"]["prompt_path"]).read_bytes()
        with database.connect(self.db) as conn:
            self.recovery = evidence.inspect_attempt_recovery(conn, self.attempt, self.task, self.driver.assigned_path)
        self.assertEqual(self.recovery["reason_code"], "pre_pr_review_deadline")

    def launch_stage(self, **kwargs):
        output = Path(kwargs["log_dir"])
        tree = Path(kwargs["worktree_path"])
        if "ROBERT_PRE_PR_CANDIDATE_DIR" in kwargs["trusted_environment"]:
            self.stage_calls.append("implement")
            (tree / "source.py").write_text("value = 3\n")
            git(tree, "commit", "-am", "implement")
            (output / "candidate.json").write_text(
                json.dumps(
                    {
                        "planned_github_actions": _new_pr_actions(),
                        "verification": [_verification_evidence()],
                        "used_skills": [],
                        "handoff": "Update verified.",
                        "consumed_event_fingerprints": [self.driver.request["event"]["event_fingerprint"]],
                    }
                )
            )
            return {"ok": True, "process_exited": True, "returncode": 0, "pid": 1073741824}
        self.stage_calls.append("review")
        return self.launcher(**kwargs)

    def prepare_resume(self, conn, recovery=None):
        return preparation.create_resume_attempt_and_prompt(
            conn,
            self.db.parent,
            self.db,
            self.driver.repo,
            "repo:example/backend",
            self.task,
            self.workstream_id,
            self.attempt,
            self.now,
            recovery or self.recovery,
        )

    def grant(self, conn):
        self.assertEqual(
            operations.classify_recovery(
                conn,
                "task",
                self.task,
                self.attempt,
                {"identity_status": "dead", **self.recovery},
                now=self.now,
            ),
            "recover_once",
        )

    def test_monitor_resumes_cached_stages_with_fresh_deadline_and_unchanged_budgets(self):
        self.checkpoint()
        with database.connect(self.db) as conn:
            monitor.supervise_running_attempts(
                conn,
                "repo:example/backend",
                {},
                self.now,
                data_dir=self.db.parent,
                db_path=self.db,
                repo=self.driver.repo,
                resume_attempt=preparation.create_resume_attempt_and_prompt,
            )
        with database.connect(self.db) as conn:
            saved = store.session(conn, task_id=self.task)
            self.assertIsNone(saved["deadline_at"])
            self.assertEqual(saved["request"], self.request)
            self.assertEqual(store.runs(conn, self.driver.round_id), self.original_runs)
            self.assertEqual(execution_control.get_binding(conn, "task", self.task)["recovery_attempts"], 1)
            resumed = conn.execute(
                "SELECT attempt_id,metadata_json FROM attempts WHERE task_id=? ORDER BY attempt_no DESC LIMIT 1",
                (self.task,),
            ).fetchone()
            self.assertNotEqual(resumed[0], self.attempt)
            self.assertEqual(json.loads(resumed[1])["resume"]["previous_attempt_id"], self.attempt)
        restarted = Coordinator(self.db, self.task, resumed[0], launcher=self.launch_stage, poll_seconds=0.01)
        with patch.object(restarted, "no_repair_disposition", side_effect=StageStopped("reached cached review")):
            with self.assertRaisesRegex(StageStopped, "reached cached review"):
                restarted.run()
        self.assertEqual(self.stage_calls, ["implement", "review"])
        with database.connect(self.db) as conn:
            saved = store.session(conn, task_id=self.task)
            self.assertGreater(values.parse_time(saved["deadline_at"]), values.parse_time(self.now))
            self.assertEqual(Path(saved["request"]["prompt_path"]).read_bytes(), self.original_prompt)
            self.assertEqual(saved["request"], self.request)

    def test_renewal_requires_consumed_grant_exact_attempt_and_current_exit_evidence(self):
        self.checkpoint()
        for refusal in (
            "no_grant",
            "identity",
            "children",
            "checkpoint",
            "wrong_attempt",
            "binding_attention",
            "unrelated_attention",
        ):
            with self.subTest(refusal=refusal), database.connect(self.db) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if refusal != "no_grant":
                    self.grant(conn)
                context = dict(self.recovery)
                if refusal == "wrong_attempt":
                    context["previous_attempt_id"] = "another-attempt"
                if refusal == "binding_attention":
                    execution_control.mark_binding_attention(conn, "task", self.task, self.now)
                if refusal == "unrelated_attention":
                    store.update(conn, self.driver.round_id, error="review: worker identity requires attention")
                patches = []
                if refusal == "identity":
                    patches.append(
                        patch(
                            "robert_agent.adapters.processes.attempt.dispatch_status",
                            return_value={"status": "unknown"},
                        )
                    )
                if refusal == "children":
                    patches.append(patch.object(rounds, "exit_observation_current", return_value=False))
                if refusal == "checkpoint":
                    patches.append(
                        patch.object(evidence, "inspect_attempt_recovery", return_value={"status": "unrecoverable"})
                    )
                with ExitStack() as stack:
                    for context_patch in patches:
                        stack.enter_context(context_patch)
                    self.assertIsNone(self.prepare_resume(conn, context))
                self.assertEqual(store.session(conn, task_id=self.task)["deadline_at"], self.expired)
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM attempts WHERE task_id=?", (self.task,)).fetchone()[0], 1
                )
                conn.rollback()

    def test_renewal_rolls_back_with_new_attempt_and_cannot_replay_old_grant(self):
        self.checkpoint()
        with database.connect(self.db) as conn:
            self.grant(conn)
            resumed = self.prepare_resume(conn)
            self.assertIsNotNone(resumed)
            self.assertIsNone(self.prepare_resume(conn))
            conn.rollback()
        with database.connect(self.db) as conn:
            self.assertEqual(store.session(conn, task_id=self.task)["deadline_at"], self.expired)
            self.assertEqual(execution_control.get_binding(conn, "task", self.task)["recovery_attempts"], 0)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM attempts WHERE task_id=?", (self.task,)).fetchone()[0], 1
            )
