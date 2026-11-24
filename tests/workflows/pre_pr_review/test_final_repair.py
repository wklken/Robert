"""Final feedback and replies remain bound to registered reviewer evidence."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import MethodType, SimpleNamespace
import unittest

from tests.support import database as fixture_database
from robert_agent.execution.stages import StageCoordinator, StageStopped
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.persistence.schema import pre_pr_review as schema
from robert_agent.persistence.stores import artifacts
from robert_agent.workflows.pre_pr_review import final_repair
from robert_agent.workflows.pre_pr_review.final_verification import FinalReviewBlocked


class FinalRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "review.sqlite3"
        fixture = Path(__file__).parents[2] / "fixtures/persistence/pre_orm_5e0e952.sql"
        with fixture_database.connect(self.db) as conn:
            conn.executescript(fixture.read_text())
            schema.initialize(conn)
            self.task_id, self.attempt_id = conn.execute("SELECT task_id,attempt_id FROM attempts").fetchone()
            store.create(conn, self.task_id, {})
            self.round_id = store.session(conn, task_id=self.task_id)["round_id"]
        self.report = self.write("report.md", "F1 requires a guard")
        candidate = self.write("candidate.json", {"handoff": "original implementation"})
        self.manifest = {
            "snapshot_id": "snapshot-1",
            "head_sha": "c" * 40,
            "base_sha": "b" * 40,
            "candidate": {"candidate_path": str(candidate), "candidate_sha256": store.digest(candidate)},
        }
        with store.connect(self.db) as conn:
            store.save_snapshot(conn, self.round_id, self.manifest)
            store.update(conn, self.round_id, report_path=str(self.report), report_sha256=store.digest(self.report))
            artifacts.insert_for_register(
                conn,
                f"{self.round_id}:{store.digest(self.report)}",
                self.task_id,
                self.attempt_id,
                "pre_pr_review_report",
                str(self.report),
                store.digest(self.report),
                self.report.stat().st_size,
                store.now(),
            )
        self.coordinator = SimpleNamespace(
            db_path=self.db,
            task_id=self.task_id,
            attempt_id=self.attempt_id,
            round_id=self.round_id,
            manifest=self.manifest,
            request={},
            final_feedback=None,
            final_decision_feedback=None,
        )
        self.blocked = self.blocker("1")

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value) if isinstance(value, dict) else value)
        return path

    def blocker(self, suffix):
        final_sha = suffix * 40
        disposition_path = self.write(
            f"dispositions-{suffix}.json",
            {
                "candidate_sha": self.manifest["head_sha"],
                "final_sha": final_sha,
                "report_sha256": store.digest(self.report),
                "dispositions": [{"id": "F1", "status": "fixed", "reason": "added guard"}],
            },
        )
        disposition = {
            "head": final_sha,
            "dispositions_path": str(disposition_path),
            "dispositions_sha256": store.digest(disposition_path),
        }
        identity = {
            "round_id": self.round_id,
            "snapshot_id": self.manifest["snapshot_id"],
            "candidate_sha": self.manifest["head_sha"],
            "final_sha": final_sha,
            "report_sha256": store.digest(self.report),
            "dispositions_sha256": disposition["dispositions_sha256"],
            "additional_diff_sha256": "d" * 64,
        }
        value = {
            **identity,
            "schema_version": 1,
            "recommendation": "blocked",
            "blocking_findings": ["guard still missing for empty input"],
            "checked_findings": [{"id": "F1", "status": "blocking", "reason": "empty input bypasses guard"}],
        }
        verification_path = self.write(f"verification-{suffix}.json", value)
        artifact = {
            **identity,
            "final_verification_path": str(verification_path),
            "final_verification_sha256": store.digest(verification_path),
            "input_sha256": "i" * 64,
            "model": "reviewer-model",
        }
        with store.connect(self.db) as conn:
            source = store.new_run(
                conn, self.round_id, "address_findings", "writer", self.attempt_id, self.root, "source-prompt"
            )
            verifier = store.new_run(
                conn, self.round_id, "verify_final", "reviewer", self.attempt_id, self.root, "verify-prompt"
            )
            store.update_run(conn, source["run_id"], status="succeeded", artifact_json=json.dumps(disposition))
            store.update_run(conn, verifier["run_id"], status="succeeded", artifact_json=json.dumps(artifact))
        disposition.update(run_id=source["run_id"], worker_id="writer")
        artifact.update(run_id=verifier["run_id"], worker_id="reviewer")
        return FinalReviewBlocked(artifact, value, disposition)

    def entry(self):
        with store.connect(self.db) as conn:
            return store.session(conn, task_id=self.task_id)

    def waiting(self, blocked=None):
        blocked = blocked or self.blocked
        with store.connect(self.db) as conn:
            entry = store.session(conn, task_id=self.task_id)
            store.update(conn, self.round_id, request_json=json.dumps({**entry["request"], "final_repair_count": 1}))
        decision = final_repair.handle(self.coordinator, blocked)
        self.assertIsNotNone(decision)
        feedback = self.coordinator.final_decision_feedback
        binding = {
            "round_id": self.round_id,
            "snapshot_id": self.manifest["snapshot_id"],
            "candidate_sha": self.manifest["head_sha"],
            "report_sha256": store.digest(self.report),
            "dispositions_path": blocked.disposition["dispositions_path"],
            "dispositions_sha256": blocked.disposition["dispositions_sha256"],
            "final_feedback": feedback,
        }
        with store.connect(self.db) as conn:
            store.update(
                conn,
                self.round_id,
                status="waiting_for_user",
                proof_json=json.dumps({"decision": binding, "scope": {"repo": "example/backend"}}),
            )
        return binding

    def test_feedback_records_registered_verifier_and_source_identity(self):
        self.assertIsNone(final_repair.handle(self.coordinator, self.blocked))
        feedback = self.entry()["request"]["final_feedback"]
        self.assertEqual(feedback["verification_run_id"], self.blocked.artifact["run_id"])
        self.assertEqual(feedback["verification_input_sha256"], self.blocked.artifact["input_sha256"])
        self.assertEqual(feedback["source_run_id"], self.blocked.disposition["run_id"])
        final_repair.validate_feedback(self.coordinator)

    def test_feedback_rejects_changed_bytes_before_reuse(self):
        final_repair.handle(self.coordinator, self.blocked)
        for field in ("verification_path", "dispositions_path"):
            with self.subTest(field=field):
                path = Path(self.coordinator.final_feedback[field])
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                with self.assertRaises(StageStopped):
                    final_repair.validate_feedback(self.coordinator)
                path.write_bytes(original)

    def test_completed_address_cache_rejects_mutated_feedback_before_registration(self):
        final_repair.handle(self.coordinator, self.blocked)
        prompt = self.write("cached-address.md", "address the pinned final blocker")
        cached_artifact = {"head": self.blocked.disposition["head"], "model": "writer-model"}
        with store.connect(self.db) as conn:
            cached = store.new_run(
                conn,
                self.round_id,
                "address_findings",
                "writer",
                self.attempt_id,
                self.root,
                store.digest(prompt),
                model="writer-model",
            )
            store.update_run(conn, cached["run_id"], status="succeeded", artifact_json=json.dumps(cached_artifact))
        driver = self.coordinator
        driver.store = store
        driver.workspace = SimpleNamespace(verify_snapshot=lambda manifest: True)
        driver.voter_stage = "review"
        driver.sealed_output = lambda run: self.root
        driver.validate_output = MethodType(StageCoordinator.validate_output, driver)
        registered = []
        driver.register = lambda run, artifact: registered.append(run["run_id"]) or artifact

        def validate(output, model):
            final_repair.validate_feedback(driver)
            return deepcopy(cached_artifact)

        worker = {"name": "writer", "default_model": "writer-model"}
        self.assertEqual(
            StageCoordinator.run_phase(driver, "address_findings", worker, prompt, validate), cached_artifact
        )
        self.assertEqual(registered, [cached["run_id"]])
        for field in ("verification_path", "dispositions_path"):
            with self.subTest(field=field):
                path = Path(driver.final_feedback[field])
                raw = path.read_bytes()
                path.write_bytes(raw + b"\n")
                with self.assertRaises(StageStopped):
                    StageCoordinator.run_phase(driver, "address_findings", worker, prompt, validate)
                self.assertEqual(registered, [cached["run_id"]])
                path.write_bytes(raw)

    def test_feedback_rejects_changed_registered_metadata(self):
        final_repair.handle(self.coordinator, self.blocked)
        changes = (
            (self.blocked.artifact["run_id"], {"status": "failed"}),
            (self.blocked.artifact["run_id"], {"status": "superseded"}),
            (self.blocked.artifact["run_id"], {"stage": "review"}),
            (self.blocked.artifact["run_id"], {"slot_reserved": 1}),
            (self.blocked.artifact["run_id"], {"artifact": {"input_sha256": "different"}}),
            (self.blocked.artifact["run_id"], {"artifact": {"final_sha": "wrong"}}),
            (self.blocked.disposition["run_id"], {"status": "running"}),
            (self.blocked.disposition["run_id"], {"stage": "implement"}),
            (self.blocked.disposition["run_id"], {"artifact": {"head": "wrong"}}),
        )
        for run_id, fields in changes:
            with self.subTest(fields=fields, run=run_id):
                with store.connect(self.db) as conn:
                    original = next(run for run in store.runs(conn, self.round_id) if run["run_id"] == run_id)
                    updates = dict(fields)
                    if "artifact" in updates:
                        updates["artifact_json"] = json.dumps({**original["artifact"], **updates.pop("artifact")})
                    store.update_run(conn, run_id, **updates)
                with self.assertRaises(StageStopped):
                    final_repair.validate_feedback(self.coordinator)
                with store.connect(self.db) as conn:
                    store.update_run(
                        conn,
                        run_id,
                        status=original["status"],
                        stage=original["stage"],
                        slot_reserved=original["slot_reserved"],
                        artifact_json=json.dumps(original["artifact"]),
                    )

    def test_waiting_reply_rejects_changed_final_evidence(self):
        binding = self.waiting()
        store.validate_reply_checkpoint(self.entry(), binding)
        path = Path(self.blocked.artifact["final_verification_path"])
        path.write_text(path.read_text() + "\n")
        with store.connect(self.db) as conn, self.assertRaises(ValueError):
            store.accept_reply(conn, store.session(conn, task_id=self.task_id), binding, "clarify empty input")
        self.assertNotIn("operator_reply", self.entry()["request"])

    def test_waiting_feedback_is_current_blocker_not_first_repair_feedback(self):
        final_repair.handle(self.coordinator, self.blocked)
        second = self.blocker("2")
        binding = self.waiting(second)
        self.assertEqual(binding["final_feedback"]["final_sha"], second.disposition["head"])
        self.assertEqual(binding["final_feedback"]["verification_run_id"], second.artifact["run_id"])
        store.validate_reply_checkpoint(self.entry(), binding)

    def test_reply_and_rebind_preserve_one_repair_limit(self):
        final_repair.handle(self.coordinator, self.blocked)
        second = self.blocker("2")
        binding = self.waiting(second)
        with store.connect(self.db) as conn:
            store.accept_reply(conn, store.session(conn, task_id=self.task_id), binding, "keep original scope")
            saved = store.session(conn, task_id=self.task_id)
            store.rebind_for_retry(conn, saved, self.task_id, self.attempt_id)
        saved = self.entry()
        self.assertEqual(saved["request"]["final_repair_count"], 1)
        self.assertEqual(saved["proof"], {})
        self.assertEqual(saved["request"]["final_feedback"], binding["final_feedback"])
        self.assertIn(second.artifact["run_id"], saved["feedback_runs"])
        store.validate_reply_checkpoint(saved, saved["request"]["operator_reply"]["decision"])
        self.coordinator.request = deepcopy(saved["request"])
        self.coordinator.final_feedback = self.coordinator.request["final_feedback"]
        final_repair.validate_feedback(self.coordinator)
        self.assertIsNotNone(final_repair.handle(self.coordinator, second))
        self.assertEqual(self.entry()["request"]["final_repair_count"], 1)

    def test_rebound_reply_loads_its_runs_even_with_previous_request_feedback(self):
        final_repair.handle(self.coordinator, self.blocked)
        previous_feedback = deepcopy(self.coordinator.final_feedback)
        second = self.blocker("2")
        binding = self.waiting(second)
        with store.connect(self.db) as conn:
            store.accept_reply(conn, store.session(conn, task_id=self.task_id), binding, "clarify latest blocker")
            saved = store.session(conn, task_id=self.task_id)
            store.rebind_for_retry(conn, saved, self.task_id, self.attempt_id)
            request = store.session(conn, task_id=self.task_id)["request"]
            request["final_feedback"] = previous_feedback
            store.update(conn, self.round_id, request_json=json.dumps(request))
        saved = self.entry()
        self.assertIn(second.artifact["run_id"], saved["feedback_runs"])
        store.validate_reply_checkpoint(saved, binding)

    def test_waiting_reply_rejects_changed_registered_verifier(self):
        binding = self.waiting()
        with store.connect(self.db) as conn:
            store.update_run(conn, self.blocked.artifact["run_id"], status="failed")
        with store.connect(self.db) as conn, self.assertRaises(ValueError):
            store.accept_reply(conn, store.session(conn, task_id=self.task_id), binding, "clarify scope")

    def refresh_after_reply(self):
        final_repair.handle(self.coordinator, self.blocked)
        old_binding = self.waiting()
        with store.connect(self.db) as conn:
            store.accept_reply(conn, store.session(conn, task_id=self.task_id), old_binding, "clarify original scope")
            request = store.session(conn, task_id=self.task_id)["request"]
            request.pop("final_feedback")
            store.update(conn, self.round_id, request_json=json.dumps(request))
            self.manifest = {
                **self.manifest,
                "snapshot_id": "snapshot-2",
                "head_sha": self.blocked.disposition["head"],
                "base_sha": "n" * 40,
            }
            store.save_snapshot(conn, self.round_id, self.manifest)
        self.coordinator.manifest = self.manifest
        self.coordinator.request = request
        self.coordinator.final_feedback = None
        self.coordinator.final_decision_feedback = None
        self.report = self.write("report-2.md", "new review of the preserved repair")
        with store.connect(self.db) as conn:
            store.update(conn, self.round_id, report_path=str(self.report), report_sha256=store.digest(self.report))
        return old_binding

    def test_new_snapshot_replaces_feedback_without_resetting_repair_limit(self):
        old_binding = self.refresh_after_reply()
        final_repair.validate_feedback(self.coordinator)
        with self.assertRaises(ValueError):
            store.validate_reply_checkpoint(self.entry(), old_binding)
        latest = self.blocker("2")
        binding = self.waiting(latest)
        self.assertEqual(binding["final_feedback"]["snapshot_id"], "snapshot-2")
        self.assertEqual(binding["final_feedback"]["candidate_sha"], self.manifest["head_sha"])
        store.validate_reply_checkpoint(self.entry(), binding)
        self.assertEqual(self.entry()["request"]["final_repair_count"], 1)
        self.assertEqual(self.entry()["request"]["operator_reply"]["decision"], old_binding)

    def test_restart_preserves_historical_reply_as_context_only(self):
        binding = self.refresh_after_reply()
        store.validate_reply_context(self.entry(), binding)
        with self.assertRaises(ValueError):
            store.validate_reply_checkpoint(self.entry(), binding)
        with store.connect(self.db) as conn, self.assertRaises(ValueError):
            store.accept_reply(conn, store.session(conn, task_id=self.task_id), binding, "new unauthorized reply")
        self.assertEqual(self.entry()["request"]["final_repair_count"], 1)

    def test_historical_context_rejects_reply_not_bound_in_authenticated_request(self):
        binding = self.refresh_after_reply()
        changed = deepcopy(binding)
        changed["candidate_sha"] = self.manifest["head_sha"]
        with self.assertRaises(ValueError):
            store.validate_reply_context(self.entry(), changed)

    def test_historical_reply_rejects_changed_report_candidate_and_feedback_bytes(self):
        binding = self.refresh_after_reply()
        for path in (
            self.root / "report.md",
            self.root / "candidate.json",
            Path(self.blocked.artifact["final_verification_path"]),
            Path(self.blocked.disposition["dispositions_path"]),
        ):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                with self.assertRaises(ValueError):
                    store.validate_reply_context(self.entry(), binding)
                path.write_bytes(original)

    def test_historical_reply_rejects_changed_registered_verifier(self):
        binding = self.refresh_after_reply()
        with store.connect(self.db) as conn:
            store.update_run(conn, self.blocked.artifact["run_id"], status="failed")
        with self.assertRaises(ValueError):
            store.validate_reply_context(self.entry(), binding)

    def test_historical_reply_requires_saved_snapshot_and_registered_report(self):
        binding = self.refresh_after_reply()
        with fixture_database.connect(self.db) as conn:
            conn.execute(
                "DELETE FROM artifacts WHERE artifact_id=?",
                (f"{self.round_id}:{binding['report_sha256']}",),
            )
        with self.assertRaises(ValueError):
            store.validate_reply_context(self.entry(), binding)

    def test_historical_reply_rejects_changed_saved_snapshot_identity(self):
        binding = self.refresh_after_reply()
        with fixture_database.connect(self.db) as conn:
            row = conn.execute(
                "SELECT manifest_json FROM pre_pr_review_snapshots WHERE snapshot_id=?", (binding["snapshot_id"],)
            ).fetchone()
            manifest = json.loads(row[0])
            manifest["head_sha"] = self.manifest["head_sha"]
            conn.execute(
                "UPDATE pre_pr_review_snapshots SET manifest_json=? WHERE snapshot_id=?",
                (json.dumps(manifest), binding["snapshot_id"]),
            )
        with self.assertRaises(ValueError):
            store.validate_reply_context(self.entry(), binding)

    def test_unregistered_verifier_cannot_consume_repair_limit(self):
        self.blocked.artifact["run_id"] = "unregistered-run"
        with self.assertRaises(StageStopped):
            final_repair.handle(self.coordinator, self.blocked)
        self.assertNotIn("final_repair_count", self.entry()["request"])
