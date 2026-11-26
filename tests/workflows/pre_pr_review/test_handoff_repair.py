"""Artifact repairs preserve source, effects and recorded verification evidence."""

from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from robert_agent.adapters.git.review_revision import clean_head, git
from robert_agent.delivery import contract
from robert_agent.execution.stages import StageCoordinator, StageStopped
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.persistence.schema import pre_pr_review as schema
from robert_agent.workflows.pre_pr_review import handoff_repair
from robert_agent.workflows.review import files
from tests.support import database as fixture_database


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
        raise AssertionError("deterministic handoff repair must not launch a model")

    def _wait(self, run, launched, timeout):
        pass

    def address_workspace(self, run_id):
        return self.address_path


class HandoffRepairTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "feature")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "source.py").write_text("value = 2\n")
        git(self.repo, "add", "source.py")
        git(self.repo, "commit", "-m", "Fixture source")
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.db = self.root / "repair.sqlite3"
        fixture = Path(__file__).parents[2] / "fixtures/persistence/pre_orm_5e0e952.sql"
        with fixture_database.connect(self.db) as conn:
            conn.executescript(fixture.read_text())
            schema.initialize(conn)
            conn.execute(
                "CREATE TABLE model_usage(kind TEXT, call_id TEXT, scope_id TEXT, recorded_at TEXT, "
                "usage_json TEXT, PRIMARY KEY(kind,call_id))"
            )
            self.task, self.attempt = conn.execute("SELECT task_id,attempt_id FROM attempts").fetchone()
            store.create(conn, self.task, {})
        self.driver = RepairDriver(self.db, self.task, self.attempt)
        self.driver.manifest = {"artifact_root": str(self.root / "artifacts"), "worktree_path": str(self.repo)}
        Path(self.driver.manifest["artifact_root"]).mkdir()
        self.driver.assigned_path = str(self.repo)
        self.driver.branch = "feature"
        self.driver.address_path = self.root / "address"
        self.driver.implementer = {
            "name": "writer",
            "agent": "codex",
            "default_model": "repair-model",
            "timeout_seconds": 4200,
        }
        self.driver.launches = []
        self.driver.on_validate = None
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
        if stage == "address_findings" and not self.driver.address_path.exists():
            git(self.repo, "worktree", "add", "--detach", str(self.driver.address_path), "HEAD")
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
        if self.driver.on_validate:
            self.driver.on_validate(output / name)
        value = json.loads((output / name).read_text())
        if self.run["stage"] == "implement":
            contract.validate_submission(value.get("submission", value), value.get("output_type", "new_pr"), "github")
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

    def forge_during_validation(self):
        self.driver.on_validate = lambda path: path.write_text(json.dumps(self.driver.repaired))

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
        self.forge_during_validation()
        with self.assertRaises(StageStopped):
            self.repair()

    def test_supported_correction_requires_no_model_call_or_new_run(self):
        with store.connect(self.db) as conn:
            before = store.runs(conn, self.driver.round_id)
        with fixture_database.connect(self.db) as conn:
            before_usage = conn.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0]
        result = self.repair()
        self.assertEqual(self.driver.launches, [])
        self.assertEqual(json.loads(Path(result["result_path"]).read_text()), self.driver.repaired)
        self.assertEqual(result["handoff_repair_sha256"], store.digest(result["handoff_repair_path"]))
        self.assertEqual(result["model"], "writer-model")
        with store.connect(self.db) as conn:
            after = store.runs(conn, self.driver.round_id)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0], before_usage)
        self.assertEqual([run["run_id"] for run in after], [run["run_id"] for run in before])

    def test_recorded_skip_explanation_requires_no_model_call(self):
        original = deepcopy(self.value)
        original["verification"][0].update(
            status="skipped", required=False, source="worker_reported", purpose="Skipped: optional CI unavailable"
        )
        original["verification"][0].pop("exit_code")
        self.run = self.make_run("implement", original)
        self.driver.repaired = deepcopy(original)
        self.driver.repaired.pop("unsupported")
        self.driver.repaired["verification"][0]["skipped_reason"] = "optional CI unavailable"
        result = self.repair(
            contract.InvalidSubmission("verification[0].skipped_reason", "Skipped verification requires a reason.")
        )
        self.assertEqual(self.driver.launches, [])
        self.assertEqual(json.loads(Path(result["result_path"]).read_text()), self.driver.repaired)

    def test_worker_envelope_is_preserved_during_nested_submission_repair(self):
        original = {
            "task_id": self.task,
            "attempt_id": self.attempt,
            "output_type": "new_pr",
            "submission": deepcopy(self.value),
        }
        self.run = self.make_run("implement", original)
        self.driver.repaired = deepcopy(original)
        del self.driver.repaired["submission"]["unsupported"]
        result = self.repair()
        self.assertEqual(json.loads(Path(result["result_path"]).read_text()), self.driver.repaired)
        self.assertEqual(self.driver.launches, [])

    def test_worker_envelope_identity_and_nested_evidence_cannot_be_rewritten(self):
        original = {
            "task_id": self.task,
            "attempt_id": self.attempt,
            "output_type": "new_pr",
            "submission": deepcopy(self.value),
        }
        self.run = self.make_run("implement", original)
        self.forge_during_validation()
        for field in ("task_id", "attempt_id", "output_type", "ci_url"):
            with self.subTest(field=field):
                self.run = self.make_run("implement", original)
                self.driver.repaired = deepcopy(original)
                del self.driver.repaired["submission"]["unsupported"]
                if field == "ci_url":
                    self.driver.repaired["submission"]["verification"][0]["url"] = "https://ci.invalid/forged"
                else:
                    self.driver.repaired[field] = "forged"
                with self.assertRaises(StageStopped):
                    self.repair()

    def test_supported_fields_and_verification_provenance_are_preserved(self):
        changes = [
            ("handoff", "Changed"),
            ("consumed_work_item_event_ids", ["forged"]),
            ("used_skills", ["invented"]),
            ("consumed_event_fingerprints", []),
        ]
        self.forge_during_validation()
        for field, value in changes:
            with self.subTest(field=field):
                self.run = self.make_run("implement", self.value)
                self.driver.repaired = {**deepcopy(self.value), field: value}
                del self.driver.repaired["unsupported"]
                with self.assertRaises(StageStopped):
                    self.repair()

    def test_source_commit_during_repair_is_rejected(self):
        def commit():
            (self.repo / "source.py").write_text("value = 99\n")
            git(self.repo, "commit", "-am", "forbidden source repair")

        self.driver.on_validate = lambda path: commit()
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
        self.driver.on_validate = lambda path: (self.repo / "source.py").write_text("value = 99\n")
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
        self.assertEqual(self.driver.launches, [])

    def test_address_writer_commit_and_finding_changes_are_rejected(self):
        value = {
            "candidate_sha": self.head,
            "final_sha": self.head,
            "report_sha256": "report",
            "body": "Original body",
            "dispositions": [{"id": "F1", "status": "fixed", "reason": "guard"}],
            "verification": deepcopy(self.value["verification"]),
        }
        for field in ("body", "id", "purpose", "commit"):
            with self.subTest(field=field):
                self.run = self.make_run("address_findings", value)
                self.driver.repaired = deepcopy(value)
                self.forge_during_validation()
                if field == "body":
                    self.driver.repaired["body"] = "forged body"
                elif field == "id":
                    self.driver.repaired["dispositions"][0]["id"] = "forged finding"
                elif field == "purpose":
                    self.driver.repaired["verification"][0]["purpose"] = "forged purpose"
                else:

                    def commit():
                        (self.driver.address_path / "source.py").write_text("value = 99\n")
                        git(self.driver.address_path, "commit", "-am", "forbidden address repair")

                    self.driver.on_validate = lambda path: commit()
                with self.assertRaises(StageStopped):
                    self.repair(
                        contract.InvalidSubmission("verification[0]", "Verification contains unsupported fields.")
                    )

    def test_action_body_change_is_rejected_even_during_unsupported_cleanup(self):
        self.driver.repaired["planned_github_actions"][0]["body"] = "Changed body"
        self.forge_during_validation()
        with self.assertRaises(StageStopped):
            self.repair()

    def test_local_correction_preserves_source_failure_and_resume_is_stable(self):
        with store.connect(self.db) as conn:
            store.update_run(conn, self.run["run_id"], status="failed", error="original validation failure")
        first = self.repair()
        second = self.repair()
        self.assertEqual(first, second)
        self.assertEqual(self.driver.launches, [])
        with store.connect(self.db) as conn:
            source = next(run for run in store.runs(conn, self.driver.round_id) if run["run_id"] == self.run["run_id"])
        self.assertEqual(source["status"], "failed")
        self.assertEqual(source["error"], "original validation failure")

    def test_original_validation_failure_stops_without_model_or_coding_launch(self):
        def refuse(path):
            raise RuntimeError("original validator refuses missing proof")

        self.driver.on_validate = refuse
        for _ in range(2):
            with self.assertRaises(StageStopped):
                self.repair()
        self.assertEqual(self.driver.launches, [])

    def test_changed_head_on_resume_cannot_repin_success(self):
        self.repair()
        (self.repo / "source.py").write_text("value = 99\n")
        git(self.repo, "commit", "-am", "source changed after repair")
        with self.assertRaises(StageStopped):
            self.repair()
        self.assertEqual(self.driver.launches, [])

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

    def test_verification_evidence_cannot_be_fabricated_during_validation(self):
        for field, value in (
            ("status", "skipped"),
            ("exit_code", 1),
            ("required", False),
            ("source", "worker_reported"),
            ("command", "invented command"),
            ("command_record_path", "/forged/journal.json"),
            ("skipped_reason", "invented dependency"),
        ):
            with self.subTest(field=field):
                self.run = self.make_run("implement", self.value)
                self.driver.repaired = deepcopy(self.value)
                self.driver.repaired.pop("unsupported")
                self.driver.repaired["verification"][0][field] = value
                self.forge_during_validation()
                with self.assertRaises(StageStopped):
                    self.repair()
        self.assertEqual(self.driver.launches, [])

    def test_deterministic_cached_candidate_tamper_is_not_overwritten(self):
        result = self.repair()
        target = Path(result["handoff_repair_path"])
        value = json.loads(target.read_text())
        value["verification"][0]["url"] = "https://ci.invalid/forged"
        target.write_text(json.dumps(value))
        forged = target.read_bytes()
        with self.assertRaises(StageStopped):
            self.repair()
        self.assertEqual(target.read_bytes(), forged)
        self.assertEqual(self.driver.launches, [])

    def test_source_prompt_and_input_tamper_during_validation_are_refused(self):
        for field in ("source", "prompt", "input"):
            with self.subTest(field=field):
                self.run = self.make_run("implement", self.value)
                original_prompt = self.prompt_path.read_bytes()
                sealed_source = self.driver.sealed_output(self.run) / "candidate.json"

                def tamper(target):
                    path = (
                        sealed_source
                        if field == "source"
                        else self.prompt_path
                        if field == "prompt"
                        else Path(self.driver.handoff_repair_input)
                    )
                    path.write_bytes(path.read_bytes() + b"\n")

                self.driver.on_validate = tamper
                with self.assertRaises(StageStopped):
                    self.repair()
                self.prompt_path.write_bytes(original_prompt)
        self.assertEqual(self.driver.launches, [])

    def test_duplicate_nonfinite_and_nonobject_json_are_refused(self):
        source = Path(self.run["output_dir"], "candidate.json")
        for document in ('{"handoff":"original","handoff":"forged"}', '{"unsupported":NaN}', "[]", "{broken}"):
            with self.subTest(document=document):
                self.run = self.make_run("implement", self.value)
                source = Path(self.run["output_dir"], "candidate.json")
                source.write_text(document)
                with self.assertRaises(StageStopped):
                    self.repair()
        self.assertEqual(self.driver.launches, [])
