"""Artifact repairs preserve source, effects and recorded verification evidence."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from robert_agent.adapters.git.review_revision import clean_head
from robert_agent.delivery import contract
from robert_agent.execution.stages import StageCoordinator, StageStopped
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.persistence.schema import pre_pr_review as schema
from robert_agent.workflows.pre_pr_review import handoff_repair
from robert_agent.workflows.review import files
from tests.support import database as fixture_database
from tests.support.review_workspace import ReviewWorkspaceFixture


class RepairDriver(StageCoordinator):
    store = store
    workspace = SimpleNamespace(verify_snapshot=lambda manifest: True)
    artifact_files = files
    handoff_files = {
        "implement": ("candidate.json",),
        "address_findings": ("dispositions.json",),
        "repair_handoff": ("handoff.json",),
    }
    artifact_kinds = ()
    voter_stage = "review"
    workflow_name = "pre_pr_review"

    def check_active(self):
        pass

    def check_launch_budget(self, stage, prompt_hash, previous):
        if sum(run["status"] == "failed" for run in previous) >= 3:
            raise StageStopped("repair retry budget exhausted")

    def stage_environment(self, run):
        return {}

    def launch_with_slot(self, run, worker, prompt, environment):
        self.launches.append(run["stage"])
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary model transport failure")
        if self.on_launch:
            self.on_launch()
        Path(run["output_dir"], "handoff.json").write_text(json.dumps(self.repaired))
        with store.connect(self.db_path) as conn:
            store.update_run(conn, run["run_id"], status="running", dispatch_json="{}")
        return {"ok": True, "process_exited": True}

    def _wait(self, run, launched, timeout):
        pass

    def address_workspace(self, run_id):
        return self.address_path


class HandoffRepairTests(ReviewWorkspaceFixture):
    def setUp(self):
        super().setUp()
        self.git(self.repo, "config", "user.email", "test@example.invalid")
        self.git(self.repo, "config", "user.name", "Test")
        self.db = self.root / "repair.sqlite3"
        fixture = Path(__file__).parents[2] / "fixtures/persistence/pre_orm_5e0e952.sql"
        with fixture_database.connect(self.db) as conn:
            conn.executescript(fixture.read_text())
            schema.initialize(conn)
            self.task, self.attempt = conn.execute("SELECT task_id,attempt_id FROM attempts").fetchone()
            store.create(conn, self.task, {})
        self.driver = RepairDriver(self.db, self.task, self.attempt)
        self.driver.manifest = {"artifact_root": str(self.root / "artifacts")}
        Path(self.driver.manifest["artifact_root"]).mkdir()
        self.driver.assigned_path = str(self.repo)
        self.driver.branch = "feature"
        self.driver.address_path = self.root / "address"
        self.git(self.repo, "worktree", "add", "--detach", str(self.driver.address_path), "HEAD")
        self.driver.implementer = {"name": "writer", "default_model": "repair-model", "timeout_seconds": 4200}
        self.driver.launches, self.driver.failures, self.driver.on_launch = [], 0, None
        self.value = {
            "planned_github_actions": [
                {
                    "type": "open_pr",
                    "title": "Fix",
                    "body": "Original body",
                    "issue_resolution": "resolved",
                    "remaining_scope": "none",
                }
            ],
            "used_skills": [],
            "consumed_event_fingerprints": ["event-1"],
            "consumed_work_item_event_ids": ["work-event-1"],
            "handoff": "Original handoff",
            "verification": [
                {
                    "command": "CI",
                    "status": "passed",
                    "exit_code": 0,
                    "purpose": "Original purpose",
                    "required": True,
                    "source": "external_ci",
                    "url": "https://ci.invalid/original",
                }
            ],
            "unsupported": True,
        }
        self.driver.repaired = deepcopy(self.value)
        del self.driver.repaired["unsupported"]
        self.prompt_path = self.driver.prompt("original.md", "Original writer prompt")
        self.run = self.make_run("implement", self.value)

    def make_run(self, stage, value):
        with store.connect(self.db) as conn:
            run = store.new_run(
                conn,
                self.driver.round_id,
                stage,
                "writer",
                self.attempt,
                self.driver.manifest["artifact_root"],
                store.digest(self.prompt_path),
                model="writer-model",
            )
            store.update_run(
                conn, run["run_id"], status="running", dispatch_json=json.dumps({"prompt_path": str(self.prompt_path)})
            )
            run = next(item for item in store.runs(conn, self.driver.round_id) if item["run_id"] == run["run_id"])
        Path(run["output_dir"], self.driver.handoff_files[stage][0]).write_text(json.dumps(value))
        return run

    def validate(self, output, model):
        name = self.driver.handoff_files[self.run["stage"]][0]
        value = json.loads((output / name).read_text())
        if self.run["stage"] == "implement":
            contract.validate_submission(value, "new_pr", "github")
        return {
            "result_path": str(output / name),
            "result_sha256": store.digest(output / name),
            "head": clean_head(self.repo),
        }

    def repair(self, exc=None):
        return handoff_repair.repair(
            self.driver,
            self.run,
            self.validate,
            "writer-model",
            exc
            or contract.InvalidSubmission(
                "result", "Submission contains unsupported or program-owned fields.", "program_owned_field"
            ),
        )

    def test_malformed_json_cannot_forge_failed_check_as_passed(self):
        source = Path(self.run["output_dir"], "candidate.json")
        failed = deepcopy(self.value)
        failed["verification"][0].update(status="failed", exit_code=1)
        source.write_text(json.dumps(failed)[:-1])
        with self.assertRaises(json.JSONDecodeError) as captured:
            json.loads(source.read_text())
        with self.assertRaises(StageStopped):
            self.repair(captured.exception)
        self.assertEqual(self.driver.launches, [])
        self.assertFalse(handoff_repair.repairable(captured.exception))

    def test_ci_url_replacement_is_rejected(self):
        self.driver.repaired["verification"][0]["url"] = "https://ci.invalid/forged"
        with self.assertRaises(StageStopped):
            self.repair()

    def test_supported_fields_and_verification_provenance_are_preserved(self):
        changes = [
            ("handoff", "Changed"),
            ("consumed_work_item_event_ids", ["forged"]),
            ("used_skills", ["invented"]),
            ("consumed_event_fingerprints", []),
        ]
        for field, value in changes:
            with self.subTest(field=field):
                self.driver.repaired = {**deepcopy(self.value), field: value}
                del self.driver.repaired["unsupported"]
                with self.assertRaises(StageStopped):
                    self.repair()

    def test_source_commit_during_repair_is_rejected(self):
        def commit():
            (self.repo / "source.py").write_text("value = 99\n")
            self.git(self.repo, "commit", "-am", "forbidden source repair")

        self.driver.on_launch = commit
        with self.assertRaises(StageStopped):
            self.repair()

    def test_dirty_source_before_launch_is_rejected(self):
        (self.repo / "untracked.txt").write_text("untracked repair")
        with self.assertRaises(StageStopped):
            self.repair()
        self.assertEqual(self.driver.launches, [])

    def test_address_repair_pins_writer_and_assigned_tree(self):
        value = {
            "candidate_sha": self.head,
            "final_sha": self.head,
            "report_sha256": "report",
            "body": "Original body",
            "dispositions": [{"id": "F1", "status": "fixed", "reason": "guard"}],
            "verification": deepcopy(self.value["verification"]),
        }
        self.run = self.make_run("address_findings", value)
        self.driver.repaired = deepcopy(value)
        self.driver.on_launch = lambda: (self.repo / "source.py").write_text("value = 99\n")
        with self.assertRaises(StageStopped):
            self.repair(contract.InvalidSubmission("verification[0]", "Verification contains unsupported fields."))
        self.assertEqual(self.driver.handoff_repair_worktree, str(self.driver.address_path))

    def test_nested_unsupported_action_cleanup_preserves_all_supported_fields(self):
        original = deepcopy(self.driver.repaired)
        original["planned_github_actions"][0]["unsupported"] = "remove only this"
        self.run = self.make_run("implement", original)
        result = self.repair(
            contract.InvalidSubmission(
                "planned_github_actions[0]",
                "Action contains unsupported or program-owned fields.",
                "program_owned_field",
            )
        )
        self.assertEqual(json.loads(Path(result["result_path"]).read_text()), self.driver.repaired)
        self.assertEqual(self.driver.launches, ["repair_handoff"])

    def test_address_writer_commit_and_finding_changes_are_rejected(self):
        value = {
            "candidate_sha": self.head,
            "final_sha": self.head,
            "report_sha256": "report",
            "body": "Original body",
            "dispositions": [{"id": "F1", "status": "fixed", "reason": "guard"}],
            "verification": deepcopy(self.value["verification"]),
        }
        self.run = self.make_run("address_findings", value)
        for field in ("body", "id", "purpose", "commit"):
            with self.subTest(field=field):
                self.driver.repaired = deepcopy(value)
                if field == "body":
                    self.driver.repaired["body"] = "forged body"
                elif field == "id":
                    self.driver.repaired["dispositions"][0]["id"] = "forged finding"
                elif field == "purpose":
                    self.driver.repaired["verification"][0]["purpose"] = "forged purpose"
                else:

                    def commit():
                        (self.driver.address_path / "source.py").write_text("value = 99\n")
                        self.git(self.driver.address_path, "commit", "-am", "forbidden address repair")

                    self.driver.on_launch = commit
                with self.assertRaises(StageStopped):
                    self.repair(
                        contract.InvalidSubmission("verification[0]", "Verification contains unsupported fields.")
                    )

    def test_action_body_change_is_rejected_even_during_unsupported_cleanup(self):
        self.driver.repaired["planned_github_actions"][0]["body"] = "Changed body"
        with self.assertRaises(StageStopped):
            self.repair()

    def test_nested_transient_failure_retries_only_repair_and_resume_is_stable(self):
        self.driver.failures = 1
        first = self.repair()
        second = self.repair()
        self.assertEqual(first, second)
        self.assertEqual(self.driver.launches, ["repair_handoff", "repair_handoff"])
        with store.connect(self.db) as conn:
            source = next(run for run in store.runs(conn, self.driver.round_id) if run["run_id"] == self.run["run_id"])
        self.assertEqual(source["status"], "running")

    def test_transient_exhaustion_stops_without_new_coding_launch(self):
        self.driver.failures = 10
        for _ in range(2):
            with self.assertRaises(StageStopped):
                self.repair()
        self.assertEqual(self.driver.launches, ["repair_handoff"] * 3)

    def test_changed_head_on_resume_cannot_repin_success(self):
        self.repair()
        (self.repo / "source.py").write_text("value = 99\n")
        self.git(self.repo, "commit", "-am", "source changed after repair")
        with self.assertRaises(StageStopped):
            self.repair()
        self.assertEqual(self.driver.launches, ["repair_handoff"])

    def test_missing_skip_reason_cannot_be_invented(self):
        original = deepcopy(self.value)
        original["verification"][0].update(status="skipped", required=False, source="worker_reported")
        original["verification"][0].pop("exit_code")
        self.run = self.make_run("implement", original)
        self.driver.repaired = deepcopy(original)
        self.driver.repaired.pop("unsupported")
        self.driver.repaired["verification"][0]["skipped_reason"] = "invented missing dependency"
        with self.assertRaises(StageStopped):
            self.repair(
                contract.InvalidSubmission("verification[0].skipped_reason", "Skipped verification requires a reason.")
            )

    def test_skip_reason_may_only_copy_explicit_recorded_purpose(self):
        original = deepcopy(self.value)
        original["verification"][0].update(
            status="skipped",
            required=False,
            source="worker_reported",
            purpose="Skipped: optional remote CI unavailable",
        )
        original["verification"][0].pop("exit_code")
        self.run = self.make_run("implement", original)
        self.driver.repaired = deepcopy(original)
        self.driver.repaired.pop("unsupported")
        self.driver.repaired["verification"][0]["skipped_reason"] = "optional remote CI unavailable"
        result = self.repair(
            contract.InvalidSubmission("verification[0].skipped_reason", "Skipped verification requires a reason.")
        )
        self.assertEqual(json.loads(Path(result["result_path"]).read_text()), self.driver.repaired)
