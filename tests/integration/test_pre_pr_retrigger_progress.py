"""An explicit GitHub retrigger preserves safe unpublished review progress."""

import json
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import yaml

from robert_agent.adapters.git.review_revision import git
from robert_agent.delivery.application import submission
from robert_agent.execution.stages import StageStopped
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.work.application import commands, preparation
from robert_agent.workflows.pre_pr_review.coordinator import Coordinator
from robert_agent.workflows.review import workspace as review_workspace
from tests.support import database as fixture_database
from tests.support.repository_cycle import _new_pr_actions, _verification_evidence
from tests.support.review_coordinator import ReviewCoordinatorFixture


class PrePRRetriggerProgressTests(ReviewCoordinatorFixture):
    def start_checkpoint(self, *, legacy_snapshot=False, completed_repair=False):
        config = yaml.safe_load(self.config_path.read_text())
        config["repos"][0]["routes"] = {"new-pr": {"pre_pr_review": {"workers": ["claude"]}}}
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle(
            [
                {
                    "id": "original-assignment",
                    "number": 42,
                    "source_type": "issue",
                    "event_type": "assigned",
                    "actor_login": "maintainer",
                    "assigned_to": "robert-bot",
                    "assignment_actor_login": "maintainer",
                    "body": "Update the example",
                    "intent": "unclear",
                    "title": "Current source title",
                    "state": "open",
                    "html_url": "https://github.com/example/backend/issues/42",
                }
            ]
        )
        classifier_task, classifier_attempt = self.current()
        with fixture_database.connect(self.db) as conn:
            fingerprint = conn.execute("SELECT event_fingerprint FROM github_events").fetchone()[0]
        result = submission.record_result(
            self.db,
            {
                "task_id": classifier_task,
                "attempt_id": classifier_attempt,
                "output_type": "classification_result",
                "submission": {
                    "planned_github_actions": [],
                    "consumed_event_fingerprints": [fingerprint],
                    "verification": [],
                    "handoff": "Implement the example update",
                    "used_skills": [],
                    "recommended_route": "new-pr",
                    "branch_slug": "original-classifier-slug",
                },
            },
        )
        self.assertTrue(result["ok"], result)
        self.cycle([])
        task, attempt = self.current()
        self.assertNotEqual(task, classifier_task)
        with fixture_database.connect(self.db) as conn:
            registered = store.session(conn, task_id=task)
            registered["request"]["plan"].pop("review_report_mode", None)
            if legacy_snapshot:
                # These cases recover rounds frozen before the single-round policy.
                registered["request"]["plan"].pop("review_policy")
            store.update(conn, registered["round_id"], request_json=json.dumps(registered["request"]))
        self.driver = Coordinator(self.db, task, attempt, launcher=self.launch_stage, poll_seconds=0.01)
        self.assertEqual(self.driver.branch, "codex/dd-42-original-classifier-slug")
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
        self.stage_calls = []
        self.repair_address = completed_repair
        prepare_snapshot = review_workspace.prepare_candidate_snapshot

        def prepare_legacy_snapshot(*args, **kwargs):
            kwargs.pop("original_request", None)
            manifest = prepare_snapshot(*args, **kwargs)
            with fixture_database.connect(self.db) as conn:
                implemented = next(run for run in store.runs(conn, self.driver.round_id) if run["stage"] == "implement")
            manifest["candidate"] = {
                **implemented["artifact"],
                "run_id": implemented["run_id"],
                "worker_id": implemented["worker_id"],
            }
            manifest["github_account"] = self.driver.repo["github_account"]
            manifest["decision_owner"] = self.driver.scope.get("decision_owner")
            return self.git_fixture.freeze(manifest)

        legacy = (
            patch.object(review_workspace, "prepare_candidate_snapshot", side_effect=prepare_legacy_snapshot)
            if legacy_snapshot
            else nullcontext()
        )
        interrupted = "finalize" if completed_repair else "address"
        with legacy, patch.object(self.driver, interrupted, side_effect=StageStopped("interrupted at checkpoint")):
            with self.assertRaisesRegex(StageStopped, "interrupted at checkpoint"):
                self.driver.run()
        with fixture_database.connect(self.db) as conn:
            self.checkpoint = store.session(conn, task_id=task)
            self.assertTrue(self.checkpoint["manifest"]["candidate"])
            self.assertIsNone(self.checkpoint["final_result_id"])
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM worker_results WHERE task_id=?", (task,)).fetchone()[0], 0
            )
            self.original_artifacts = {
                Path(row[0]): Path(row[0]).read_bytes()
                for row in conn.execute(
                    "SELECT path FROM artifacts WHERE task_id=? AND artifact_type IN "
                    "('prompt','github_context_json','github_context_md')",
                    (task,),
                )
            }
            self.original_runs = store.runs(conn, self.driver.round_id)
            self.assertEqual(
                [run["status"] for run in self.original_runs], ["succeeded"] * (3 if completed_repair else 2)
            )
            metadata = json.loads(
                conn.execute("SELECT metadata_json FROM attempts WHERE attempt_id=?", (attempt,)).fetchone()[0]
            )
            metadata["dispatch"] = {"pid": 1073741824, "process_exited": True, "returncode": 0}
            conn.execute(
                "UPDATE attempts SET status='failed',metadata_json=? WHERE attempt_id=?",
                (json.dumps(metadata), attempt),
            )
            conn.execute("UPDATE tasks SET lifecycle='failed' WHERE task_id=?", (task,))
            conn.execute(
                "UPDATE workstreams SET lifecycle='failed',active_task_id=NULL WHERE active_task_id=?", (task,)
            )
            conn.execute("UPDATE execution_subjects SET binding_status='terminal' WHERE subject_id=?", (task,))
        self.context = commands.CommandContext(
            "operator", "owner", frozenset({"repo:example/backend"}), frozenset({"codex", "claude"})
        )

    def launch_stage(self, **kwargs):
        prompt = Path(kwargs["prompt_path"])
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
                        "consumed_event_fingerprints": [self.driver.request["event"]["event_fingerprint"]],
                        "handoff": "Updated example and verified the result.",
                    }
                )
            )
        elif prompt.name.startswith("address-"):
            self.stage_calls.append("address")
            report_sha = prompt.stem.removeprefix("address-").split("-")[0]
            candidate_sha = git(tree, "rev-parse", "HEAD")
            if self.repair_address:
                (tree / "source.py").write_text("value = 4\n")
                git(tree, "commit", "-am", "address completed repair")
                self.repair_address = False
            (output / "dispositions.json").write_text(
                json.dumps(
                    {
                        "candidate_sha": candidate_sha,
                        "final_sha": git(tree, "rev-parse", "HEAD"),
                        "report_sha256": report_sha,
                        "dispositions": [],
                        "verification": [_verification_evidence()],
                        "body": "Updated example.\n\n## Assumptions and decisions\nNone.",
                    }
                )
            )
        else:
            self.stage_calls.append("review")
            return self.launcher(**kwargs)
        return {"ok": True, "process_exited": True, "returncode": 0, "pid": 1073741824}

    def request_retrigger(self):
        result = commands.retrigger_failed_github_task(
            self.db,
            self.driver.task_id,
            context=self.context,
            idempotency_key="recover-review",
            notifier=lambda _: None,
        )
        self.assertTrue(result["ok"], result)
        return result

    def prepare_retrigger(self):
        prepare = preparation.prepare_pre_pr_retrigger_worktree
        with patch.object(
            preparation,
            "prepare_pre_pr_retrigger_worktree",
            side_effect=lambda repo, review, event, route, dry_run: prepare(repo, review, event, route, False),
        ):
            self.cycle([])
        return self.current()

    def retrigger(self):
        self.request_retrigger()
        return self.prepare_retrigger()

    def assert_original_artifacts(self):
        for path, content in self.original_artifacts.items():
            self.assertEqual(path.read_bytes(), content, str(path))

    def test_command_retrigger_reuses_original_branch_round_and_successful_review(self):
        self.start_checkpoint()
        new_task, new_attempt = self.retrigger()
        self.assertNotEqual(new_task, self.driver.task_id)
        with fixture_database.connect(self.db) as conn:
            attempt = conn.execute(
                "SELECT branch_name,worktree_path FROM attempts WHERE attempt_id=?", (new_attempt,)
            ).fetchone()
            self.assertEqual(tuple(attempt), (self.driver.branch, self.driver.assigned_path))
            resumed = store.session(conn, task_id=new_task)
            self.assertEqual(resumed["round_id"], self.driver.round_id)
            self.assertEqual(resumed["snapshot_id"], self.checkpoint["snapshot_id"])
            self.assertEqual(resumed["request"]["event"], self.checkpoint["request"]["event"])
            self.assertEqual(resumed["request"]["input_sha256"], self.checkpoint["request"]["input_sha256"])
            self.assertEqual(resumed["request"]["prompt_path"], self.checkpoint["request"]["prompt_path"])
            self.assertEqual(store.runs(conn, resumed["round_id"]), self.original_runs)
        self.assert_original_artifacts()
        restarted = Coordinator(self.db, new_task, new_attempt, launcher=self.launch_stage, poll_seconds=0.01)
        self.assertTrue(restarted.run()["ok"])
        self.assertEqual(self.stage_calls, ["implement", "review", "address"])
        self.assert_original_artifacts()

    def test_dirty_original_worktree_blocks_retrigger_without_losing_checkpoint(self):
        self.start_checkpoint()
        (Path(self.driver.assigned_path) / "source.py").write_text("uncommitted user changes\n")
        task, attempt = self.retrigger()
        with fixture_database.connect(self.db) as conn:
            row = conn.execute(
                "SELECT status,branch_name,worktree_path FROM attempts WHERE attempt_id=?", (attempt,)
            ).fetchone()
            self.assertEqual(tuple(row), ("failed", self.driver.branch, self.driver.assigned_path))
            self.assertIsNotNone(store.session(conn, task_id=self.driver.task_id))
        self.assertEqual((Path(self.driver.assigned_path) / "source.py").read_text(), "uncommitted user changes\n")
        self.assert_original_artifacts()

    def test_legacy_snapshot_keeps_candidate_and_re_reviews_original_request_once(self):
        self.start_checkpoint(legacy_snapshot=True)
        old = self.checkpoint["manifest"]
        self.assertNotIn("original_request", old)
        frozen_files = {path: path.read_bytes() for path in Path(old["context_dir"]).rglob("*") if path.is_file()}
        task, attempt = self.retrigger()
        restarted = Coordinator(self.db, task, attempt, launcher=self.launch_stage, poll_seconds=0.01)
        self.assertTrue(restarted.run()["ok"])
        with fixture_database.connect(self.db) as conn:
            saved = store.session(conn, task_id=task)
            self.assertEqual(saved["round_id"], self.driver.round_id)
            self.assertNotEqual(saved["snapshot_id"], old["snapshot_id"])
            self.assertEqual(saved["manifest"]["candidate"], old["candidate"])
            self.assertEqual(
                saved["manifest"]["original_request"]["sha256"], self.checkpoint["request"]["prompt_sha256"]
            )
        self.assertEqual(self.stage_calls, ["implement", "review", "review", "address"])
        for path, content in frozen_files.items():
            self.assertEqual(path.read_bytes(), content, str(path))
        self.assert_original_artifacts()

    def test_legacy_snapshot_re_reviews_completed_repair_without_reimplementing(self):
        self.start_checkpoint(legacy_snapshot=True, completed_repair=True)
        old = self.checkpoint["manifest"]
        repaired = next(run for run in self.original_runs if run["stage"] == "address_findings")
        repair_sha = repaired["artifact"]["head"]
        self.assertNotEqual(repair_sha, old["head_sha"])
        self.assertEqual(git(self.driver.assigned_path, "rev-parse", "HEAD"), old["head_sha"])
        immutable = {path: path.read_bytes() for path in Path(old["context_dir"]).rglob("*") if path.is_file()}
        for path in (old["candidate"]["candidate_path"], repaired["artifact"]["dispositions_path"]):
            immutable[Path(path)] = Path(path).read_bytes()

        task, attempt = self.retrigger()
        restarted = Coordinator(self.db, task, attempt, launcher=self.launch_stage, poll_seconds=0.01)
        self.assertTrue(restarted.run()["ok"])
        with fixture_database.connect(self.db) as conn:
            saved = store.session(conn, task_id=task)
            self.assertEqual(saved["round_id"], self.driver.round_id)
            self.assertNotEqual(saved["snapshot_id"], old["snapshot_id"])
            self.assertEqual(saved["manifest"]["head_sha"], repair_sha)
            self.assertEqual(saved["manifest"]["candidate"], {**old["candidate"], "head": repair_sha})
            resumed_runs = {run["run_id"]: run for run in store.runs(conn, saved["round_id"])}
            for run in self.original_runs:
                for key in ("artifact", "owner_attempt_id", "output_dir", "prompt_sha256"):
                    self.assertEqual(resumed_runs[run["run_id"]][key], run[key])
            self.assertEqual(saved["proof"]["head_sha"], repair_sha)
        self.assertEqual(git(self.driver.assigned_path, "rev-parse", "HEAD"), repair_sha)
        self.assertEqual((Path(self.driver.assigned_path) / "source.py").read_text(), "value = 4\n")
        self.assertEqual(self.stage_calls, ["implement", "review", "address", "review", "address"])
        for path, content in immutable.items():
            self.assertEqual(path.read_bytes(), content, str(path))
        self.assert_original_artifacts()

    def test_registered_worktree_outside_managed_root_is_refused(self):
        self.start_checkpoint()
        moved = self.root / "unmanaged-worktree"
        git(self.driver.repo["repo_root"], "worktree", "move", self.driver.assigned_path, str(moved))
        task, attempt = self.retrigger()
        with fixture_database.connect(self.db) as conn:
            row = conn.execute(
                "SELECT status,branch_name,worktree_path FROM attempts WHERE attempt_id=?", (attempt,)
            ).fetchone()
            self.assertEqual(tuple(row), ("failed", self.driver.branch, str(moved)))
            self.assertIsNotNone(store.session(conn, task_id=self.driver.task_id))
        self.assert_original_artifacts()

    def test_managed_worktree_rename_reuses_registered_path_and_original_branch(self):
        self.start_checkpoint()
        moved = Path(self.driver.repo["worktree_root"]) / "renamed-original-worktree"
        git(self.driver.repo["repo_root"], "worktree", "move", self.driver.assigned_path, str(moved))
        task, attempt = self.retrigger()
        with fixture_database.connect(self.db) as conn:
            row = conn.execute(
                "SELECT branch_name,worktree_path FROM attempts WHERE attempt_id=?", (attempt,)
            ).fetchone()
            self.assertEqual(tuple(row), (self.driver.branch, str(moved)))
            saved = store.session(conn, task_id=task)
            self.assertEqual(saved["round_id"], self.driver.round_id)
            self.assertEqual(saved["snapshot_id"], self.checkpoint["snapshot_id"])
        restarted = Coordinator(self.db, task, attempt, launcher=self.launch_stage, poll_seconds=0.01)
        self.assertTrue(restarted.run()["ok"])
        self.assertEqual(self.stage_calls, ["implement", "review", "address"])
        self.assert_original_artifacts()

    def test_derived_authorization_fields_restore_exact_input_hash(self):
        self.start_checkpoint()
        with fixture_database.connect(self.db) as conn:
            row = conn.execute("SELECT event_id,payload_json FROM github_events").fetchone()
            event = json.loads(row[1])
            event.pop("authorization_status", None)
            event.pop("creates_task", None)
            event.pop("drives_execution", None)
            conn.execute("UPDATE github_events SET payload_json=? WHERE event_id=?", (json.dumps(event), row[0]))
        task, _ = self.retrigger()
        with fixture_database.connect(self.db) as conn:
            saved = store.session(conn, task_id=task)
            self.assertEqual(saved["round_id"], self.driver.round_id)
            self.assertEqual(saved["request"]["input_sha256"], self.checkpoint["request"]["input_sha256"])
            self.assertEqual(saved["request"]["event"], self.checkpoint["request"]["event"])
        self.assert_original_artifacts()

    def assert_changed_context_starts_new_review(self, field, value):
        self.start_checkpoint()
        request = self.request_retrigger()
        with fixture_database.connect(self.db) as conn:
            row = conn.execute("SELECT payload_json FROM github_events").fetchone()
            changed = {**json.loads(row[0]), field: value}
            metadata = json.loads(
                conn.execute("SELECT metadata_json FROM wakeups WHERE wakeup_id=?", (request["wakeup_id"],)).fetchone()[
                    0
                ]
            )
            metadata["refreshed_context"] = [changed]
            conn.execute(
                "UPDATE wakeups SET metadata_json=? WHERE wakeup_id=?", (json.dumps(metadata), request["wakeup_id"])
            )
        task, _ = self.prepare_retrigger()
        with fixture_database.connect(self.db) as conn:
            saved = store.session(conn, task_id=task)
            self.assertNotEqual(saved["round_id"], self.driver.round_id)
            self.assertNotEqual(saved["request"]["input_sha256"], self.checkpoint["request"]["input_sha256"])
            self.assertEqual(saved["request"]["event"][field], value)
            self.assertEqual(
                store.session(conn, task_id=self.driver.task_id)["snapshot_id"], self.checkpoint["snapshot_id"]
            )
        self.assert_original_artifacts()

    def test_changed_source_content_does_not_reuse_review(self):
        self.assert_changed_context_starts_new_review("body", "Implement a different requirement")

    def test_changed_nested_metadata_does_not_reuse_review(self):
        self.assert_changed_context_starts_new_review("metadata", {"new_source_fact": True})

    def test_removed_worker_configuration_blocks_retrigger_dispatch(self):
        self.start_checkpoint()
        config = yaml.safe_load(self.config_path.read_text())
        config["workers"].pop("claude")
        config["routes"] = {}
        config["repos"][0]["routes"]["new-pr"]["pre_pr_review"]["workers"] = ["codex"]
        self.config_path.write_text(yaml.safe_dump(config))
        task, attempt = self.retrigger()
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM attempts WHERE attempt_id=?", (attempt,)).fetchone()[0], "canceled"
            )
            saved = store.session(conn, task_id=task)
            self.assertEqual(saved["round_id"], self.driver.round_id)
            self.assertEqual(saved["status"], "attention")
            self.assertIn("current worker configuration", saved["error"])
        self.assertEqual(self.stage_calls, ["implement", "review"])
        self.assert_original_artifacts()

    def test_revoked_trigger_trust_blocks_retrigger_dispatch(self):
        self.start_checkpoint()
        config = yaml.safe_load(self.config_path.read_text())
        config["repos"][0]["trusted_actors"] = ["another-maintainer"]
        self.config_path.write_text(yaml.safe_dump(config))
        task, attempt = self.retrigger()
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM attempts WHERE attempt_id=?", (attempt,)).fetchone()[0], "canceled"
            )
            self.assertEqual(store.session(conn, task_id=task)["round_id"], self.driver.round_id)
        self.assertEqual(self.stage_calls, ["implement", "review"])
        self.assert_original_artifacts()

    def test_missing_original_registration_does_not_create_replacement_worktree(self):
        self.start_checkpoint()
        git(self.driver.repo["repo_root"], "worktree", "remove", self.driver.assigned_path)
        task, attempt = self.retrigger()
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM attempts WHERE attempt_id=?", (attempt,)).fetchone()[0], "failed"
            )
            self.assertIsNotNone(store.session(conn, task_id=self.driver.task_id))
        self.assertFalse(Path(self.driver.assigned_path).exists())
        self.assert_original_artifacts()

    def test_operator_without_repository_scope_cannot_retrigger(self):
        self.start_checkpoint()
        self.context = commands.CommandContext("operator", "outsider", frozenset(), frozenset({"codex", "claude"}))
        with self.assertRaises(commands.WorkItemNotFoundError):
            self.request_retrigger()
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM wakeups WHERE json_extract(metadata_json,'$.kind')='github_retrigger'"
                ).fetchone()[0],
                0,
            )
            self.assertIsNotNone(store.session(conn, task_id=self.driver.task_id))
        self.assert_original_artifacts()

    def test_unknown_review_process_blocks_command_retrigger(self):
        self.start_checkpoint()
        with fixture_database.connect(self.db) as conn:
            run = store.new_run(
                conn,
                self.driver.round_id,
                "review",
                "claude",
                self.driver.attempt_id,
                self.root / "unknown-process",
                "unknown",
            )
            store.update_run(conn, run["run_id"], status="running", slot_reserved=1)
        with self.assertRaisesRegex(commands.WorkItemConflictError, "processes must be reconciled"):
            self.request_retrigger()
        self.assert_original_artifacts()
