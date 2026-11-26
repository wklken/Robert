"""Report registration failures reuse independent deterministic checkpoints."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.support import database as fixture_database
from robert_agent.execution.stages import StageStopped
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.persistence.schema import pre_pr_review as schema
from robert_agent.workflows.pre_pr_review import coordinator as module
from robert_agent.workflows.review import files, workspace
from tests.support.review_workspace import ReviewWorkspaceFixture
from tests.support.report_checkpoint import forge_redacted_report


class PrePRReportCheckpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.db = self.root / "review.sqlite3"
        fixture = Path(__file__).parents[2] / "fixtures/persistence/pre_orm_5e0e952.sql"
        with fixture_database.connect(self.db) as conn:
            conn.executescript(fixture.read_text())
            schema.initialize(conn)
            self.task_id, self.attempt_id = conn.execute("SELECT task_id,attempt_id FROM attempts").fetchone()
            store.create(conn, self.task_id, {})
            self.round_id = store.session(conn, task_id=self.task_id)["round_id"]
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.manifest = {
            "schema_version": 1,
            "round_id": self.round_id,
            "snapshot_id": "snapshot-1",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "base_ref": "main",
            "pr_number": None,
            "artifact_root": str(self.artifacts),
            "files": {},
        }
        self.result = {
            "schema_version": 1,
            "round_id": self.round_id,
            "snapshot_id": "snapshot-1",
            "head_sha": "a" * 40,
            "findings": [],
            "dismissed_findings": [],
            "coverage": {"reviewed_paths": ["source.py"], "unreviewed_paths": [], "notes": "read callers"},
            "merge_recommendation": "ready to merge",
        }
        self.records = []
        for worker in ("first", "second"):
            result = self.artifacts / "runs" / worker / "result.json"
            files.atomic_json(result, self.result)
            report = result.with_name("report.md")
            files.atomic_text(report, files.render_individual(self.result, worker, worker))
            self.records.append(
                {
                    "worker_id": worker,
                    "run_id": worker,
                    "model": worker,
                    "result_path": str(result),
                    "result_sha256": store.digest(result),
                    "report_path": str(report),
                    "report_sha256": store.digest(report),
                }
            )
        self.evaluation = {key: self.result[key] for key in ("schema_version", "round_id", "snapshot_id", "head_sha")}
        self.evaluation.update(items=[], merge_recommendation="ready to merge", merge_reason="No findings")
        evaluation_path = self.artifacts / "synthesis" / "sealed" / "evaluation-run" / "evaluation.json"
        files.atomic_json(evaluation_path, self.evaluation)
        inputs = files.write_synthesis_inputs(self.manifest, self.records)
        with store.connect(self.db) as conn:
            run = store.new_run(conn, self.round_id, "synthesis", "first", self.attempt_id, self.artifacts, "prompt")
            store.update_run(
                conn,
                run["run_id"],
                status="succeeded",
                artifact_json=json.dumps(
                    {
                        "evaluation_path": str(evaluation_path),
                        "evaluation_sha256": store.digest(evaluation_path),
                        "input_sha256": inputs["sha256"],
                    }
                ),
            )
        self.driver = SimpleNamespace(
            store=store,
            db_path=self.db,
            round_id=self.round_id,
            task_id=self.task_id,
            attempt_id=self.attempt_id,
            manifest=self.manifest,
            plan={},
            stage=None,
            check_active=Mock(),
            evaluate=Mock(return_value=deepcopy(self.evaluation)),
            public_body=module.Coordinator.public_body,
        )
        self.driver.report = MethodType(module.Coordinator.report, self.driver)

        def prompt(name, text):
            path = self.artifacts / name
            files.atomic_text(path, text)
            return path

        self.driver.prompt = prompt

    def appendix_inputs(self):
        candidate = self.artifacts / "candidate.json"
        files.atomic_json(
            candidate,
            {
                "planned_github_actions": [{"type": "open_pr", "title": "Example", "body": "Original PR body"}],
                "verification": [],
            },
        )
        self.manifest["candidate"] = {"candidate_path": str(candidate), "candidate_sha256": store.digest(candidate)}
        report, sha, _, _ = self.driver.report(self.records)
        response = self.artifacts / "dispositions.json"
        files.atomic_json(
            response,
            {
                "body": "Original PR body",
                "verification": [],
                "dispositions": [],
            },
        )
        self.driver.finalize = MethodType(module.Coordinator.finalize, self.driver)
        self.driver.candidate_submission = MethodType(module.Coordinator.candidate_submission, self.driver)
        self.driver.envelope = MethodType(module.Coordinator.envelope, self.driver)
        self.driver.validate_submission = lambda value: deepcopy(value)
        self.driver.phase = Mock()
        self.driver.base = "main"
        self.driver.base_refresh = None
        self.driver.assigned_path = str(self.root / "assigned")
        self.driver.branch = "assigned"
        self.driver.check_base = Mock(return_value=self.manifest["base_sha"])
        return (
            report,
            sha,
            {
                "head": self.manifest["head_sha"],
                "dispositions_path": str(response),
                "dispositions_sha256": store.digest(response),
            },
        )

    def test_finalize_redacts_body_without_mutating_sealed_dispositions(self):
        report, sha, disposition = self.appendix_inputs()
        path = Path(disposition["dispositions_path"])
        response = json.loads(path.read_text())
        local_path = "/data/workspace/example/backend"
        response["body"] = f"Validated in {local_path}; relevant checks passed."
        files.atomic_json(path, response)
        disposition["dispositions_sha256"] = store.digest(path)
        original = path.read_bytes()
        with patch.object(module, "clean_head", return_value=self.manifest["head_sha"]):
            with patch.object(module.submission, "record_result", return_value={"ok": True}) as submit:
                result = self.driver.finalize(report, sha, disposition)
        self.assertTrue(result["ok"])
        body = submit.call_args.args[1]["submission"]["planned_github_actions"][0]["body"]
        self.assertNotIn(local_path, body)
        self.assertIn("<local-path>", body)
        self.assertIn("relevant checks passed.", body)
        self.assertIn("## Review 报告", body)
        self.assertEqual(path.read_bytes(), original)

    def test_appendix_seal_registration_failure_retries_without_redaction_or_model_repeat(self):
        report, sha, disposition = self.appendix_inputs()
        original_report = report.read_bytes()
        original = module.redact_report
        with patch.object(module, "redact_report", wraps=original) as redactor:
            with patch.object(module, "clean_head", return_value=self.manifest["head_sha"]):
                with patch.object(store, "seal_result", side_effect=OSError("final seal DB failed")):
                    with self.assertRaisesRegex(OSError, "final seal DB failed"):
                        self.driver.finalize(report, sha, disposition)
                with patch.object(module.submission, "record_result", return_value={"ok": True}) as submit:
                    result = self.driver.finalize(report, sha, disposition)
                    body = submit.call_args.args[1]["submission"]["planned_github_actions"][0]["body"]
                    self.assertIn("Original PR body", body)
                    self.assertIn("## Review 报告", body)
                self.assertEqual(redactor.call_count, 1)
        self.assertTrue(result["ok"])
        self.driver.evaluate.assert_not_called()
        self.assertEqual(report.read_bytes(), original_report)
        with store.connect(self.db) as conn:
            self.assertEqual(len(store.runs(conn, self.round_id)), 1)

    def test_appendix_artifact_registration_failure_retries_only_registration(self):
        report, sha, disposition = self.appendix_inputs()
        original = module.redact_report
        original_register = module.db_write_artifacts.insert_for_register

        def fail_appendix_only(conn, artifact_id, task_id, attempt_id, kind, *args):
            if kind == "pre_pr_review_appendix":
                raise OSError("artifact DB failed")
            return original_register(conn, artifact_id, task_id, attempt_id, kind, *args)

        with patch.object(module, "redact_report", wraps=original) as redactor:
            with patch.object(module, "clean_head", return_value=self.manifest["head_sha"]):
                with patch.object(module.db_write_artifacts, "insert_for_register", side_effect=fail_appendix_only):
                    with self.assertRaisesRegex(OSError, "artifact DB failed"):
                        self.driver.finalize(report, sha, disposition)
                with patch.object(module.submission, "record_result", return_value={"ok": True}):
                    self.assertTrue(self.driver.finalize(report, sha, disposition)["ok"])
                self.assertEqual(redactor.call_count, 1)
        self.driver.evaluate.assert_not_called()

    def test_appendix_redaction_failure_retains_private_dispositions_and_retries_policy(self):
        report, sha, disposition = self.appendix_inputs()
        policy = module.redact_report
        calls = 0

        def unavailable_once(text):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"ok": False, "status": "blocked", "findings": [{"rule": "policy_unavailable"}]}
            return policy(text)

        with patch.object(module, "redact_report", side_effect=unavailable_once):
            with patch.object(module, "clean_head", return_value=self.manifest["head_sha"]):
                with self.assertRaisesRegex(StageStopped, "dispositions did not pass public content redaction"):
                    self.driver.finalize(report, sha, disposition)
                blocked = list(self.artifacts.glob("report-checkpoints/*/private/blocked-*.json"))
                self.assertEqual(len(blocked), 1)
                private = blocked[0].parent
                retained = {path.read_bytes() for path in private.glob("original-*")}
                self.assertIn(Path(disposition["dispositions_path"]).read_bytes(), retained)
                self.assertTrue((private / "rendered.md").is_file())
                self.assertFalse((private.parent / "public.md").exists())
                with patch.object(module.submission, "record_result", return_value={"ok": True}):
                    self.assertTrue(self.driver.finalize(report, sha, disposition)["ok"])
        self.assertEqual(calls, 2)
        self.driver.evaluate.assert_not_called()

    def test_appendix_tampered_public_report_or_dispositions_is_refused(self):
        report, sha, disposition = self.appendix_inputs()
        with patch.object(module, "clean_head", return_value=self.manifest["head_sha"]):
            with patch.object(store, "seal_result", side_effect=OSError("paused before seal")):
                with self.assertRaises(OSError):
                    self.driver.finalize(report, sha, disposition)
        for path in (report, Path(disposition["dispositions_path"])):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b" forged")
                with self.assertRaisesRegex(StageStopped, "review handoff changed"):
                    self.driver.finalize(report, sha, disposition)
                path.write_bytes(original)
        appendix = next(path for path in self.artifacts.glob("report-checkpoints/*/public.md") if path != report)
        appendix.write_text("Forged appendix")
        with self.assertRaises(ValueError):
            self.driver.finalize(report, sha, disposition)

    def test_legacy_registered_report_finalizes_without_models_and_preserves_exact_original(self):
        report, sha, disposition = self.appendix_inputs()
        legacy = self.artifacts / "report-legacy.md"
        original = report.read_bytes()
        legacy.write_bytes(original)
        with store.connect(self.db) as conn:
            store.update(conn, self.round_id, report_path=str(legacy), report_sha256=sha)
        self.driver.check_base.return_value = "c" * 40
        with patch.object(module, "clean_head", return_value=self.manifest["head_sha"]):
            with patch.object(module.submission, "record_result", return_value={"ok": True}):
                self.assertTrue(self.driver.finalize(legacy, sha, disposition)["ok"])
        self.assertEqual(legacy.read_bytes(), original)
        with store.connect(self.db) as conn:
            proof = store.session(conn, task_id=self.task_id)["proof"]
        self.assertEqual(proof["round_id"], self.round_id)
        self.assertEqual(proof["reviewed_base_sha"], self.manifest["base_sha"])
        self.assertEqual(proof["base_sha"], "c" * 40)
        self.driver.evaluate.assert_not_called()

    def test_legacy_report_wrong_registration_and_new_missing_checkpoint_are_refused(self):
        report, sha, disposition = self.appendix_inputs()
        legacy = self.artifacts / "report-legacy.md"
        legacy.write_bytes(report.read_bytes())
        with self.assertRaisesRegex(StageStopped, "legacy review report registration changed"):
            self.driver.finalize(legacy, sha, disposition)
        with store.connect(self.db) as conn:
            store.update(conn, self.round_id, report_path=str(legacy), report_sha256="0" * 64)
        with self.assertRaisesRegex(StageStopped, "legacy review report registration changed"):
            self.driver.finalize(legacy, sha, disposition)
        (report.parent / "seal.json").unlink()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.driver.finalize(report, sha, disposition)

    def test_evaluation_complete_registration_failure_retries_without_render_redaction_or_model(self):
        render = files.render_merged
        redact = module.redact_report
        original_register = module.db_write_artifacts.insert_for_register

        def fail_public_only(conn, artifact_id, task_id, attempt_id, kind, *args):
            if kind == "pre_pr_review_report":
                raise OSError("DB failed")
            return original_register(conn, artifact_id, task_id, attempt_id, kind, *args)

        with patch.object(files, "render_merged", wraps=render) as renderer:
            with patch.object(module, "redact_report", wraps=redact) as redactor:
                with patch.object(module.db_write_artifacts, "insert_for_register", side_effect=fail_public_only):
                    with self.assertRaisesRegex(OSError, "DB failed"):
                        self.driver.report(self.records)
                with store.connect(self.db) as conn:
                    self.assertIsNone(store.session(conn, task_id=self.task_id)["report_path"])
                path, sha, ids, _ = self.driver.report(self.records)
                self.assertEqual(renderer.call_count, 1)
                self.assertEqual(redactor.call_count, 1)
        self.driver.evaluate.assert_not_called()
        self.assertEqual(store.digest(path), sha)
        self.assertEqual(ids, [])
        with store.connect(self.db) as conn:
            session = store.session(conn, task_id=self.task_id)
            self.assertEqual(session["report_path"], str(path))
            self.assertEqual(session["report_sha256"], sha)
            self.assertEqual(len(store.runs(conn, self.round_id)), 1)
        with fixture_database.connect(self.db) as conn:
            artifacts = conn.execute(
                "SELECT artifact_type,path FROM artifacts WHERE task_id=?", (self.task_id,)
            ).fetchall()
        safe = [row for row in artifacts if row[0] == "pre_pr_review_report"]
        self.assertEqual([row[1] for row in safe], [str(path)])

    def test_coherent_report_forgery_after_registration_failure_is_refused_before_db_write(self):
        original = module.db_write_artifacts.insert_for_register

        def failed_report_registration(conn, artifact_id, task_id, attempt_id, kind, *args):
            if kind == "pre_pr_review_report":
                raise OSError("public registration failed")
            return original(conn, artifact_id, task_id, attempt_id, kind, *args)

        with patch.object(module.db_write_artifacts, "insert_for_register", side_effect=failed_report_registration):
            with self.assertRaisesRegex(OSError, "public registration failed"):
                self.driver.report(self.records)
        base = next(self.artifacts.glob("report-checkpoints/*"))
        forge_redacted_report(base)
        with patch.object(module.db_write_artifacts, "insert_for_register") as register:
            with self.assertRaises(ValueError):
                self.driver.report(self.records)
            register.assert_not_called()
        self.driver.evaluate.assert_not_called()

    def test_coherent_appendix_forgery_after_registration_failure_is_refused(self):
        report, sha, disposition = self.appendix_inputs()
        original = module.db_write_artifacts.insert_for_register

        def failed_appendix_registration(conn, artifact_id, task_id, attempt_id, kind, *args):
            if kind == "pre_pr_review_appendix":
                raise OSError("appendix registration failed")
            return original(conn, artifact_id, task_id, attempt_id, kind, *args)

        with patch.object(module.db_write_artifacts, "insert_for_register", side_effect=failed_appendix_registration):
            with self.assertRaisesRegex(OSError, "appendix registration failed"):
                self.driver.finalize(report, sha, disposition)
        base = next(path.parent for path in self.artifacts.glob("report-checkpoints/*/public.md") if path != report)
        forge_redacted_report(base)
        with patch.object(module.submission, "record_result") as submit:
            with patch.object(module, "clean_head", return_value=self.manifest["head_sha"]):
                with self.assertRaises(ValueError):
                    self.driver.finalize(report, sha, disposition)
            submit.assert_not_called()
        self.driver.evaluate.assert_not_called()

    def test_single_reviewer_cached_report_tamper_is_refused_before_registration(self):
        path, _, _, _ = self.driver.report(self.records[:1])
        path.write_text("Forged public report")
        with self.assertRaises((StageStopped, ValueError)):
            self.driver.report(self.records[:1])

    def test_real_candidate_snapshot_accepts_only_pinned_parent_round_sources(self):
        repository = ReviewWorkspaceFixture()
        repository.setUp()
        self.addCleanup(repository.doCleanups)
        round_root = self.artifacts / self.round_id
        candidate_path = round_root / "implement" / "sealed" / "writer-run" / "candidate.json"
        files.atomic_json(candidate_path, {"body": "Original private candidate"})
        original_path = round_root / "original-request.md"
        files.atomic_text(original_path, "Original acceptance criteria")
        original = {"path": str(original_path), "sha256": store.digest(original_path)}
        self.manifest = workspace.prepare_candidate_snapshot(
            repository.repo,
            repository.root / "worktrees",
            self.artifacts,
            self.round_id,
            "example/backend",
            "main",
            repository.base,
            repository.head,
            {"event_fingerprint": "source-event"},
            original_request=original,
        )
        self.manifest["candidate"] = {
            "candidate_path": str(candidate_path),
            "candidate_sha256": store.digest(candidate_path),
        }
        self.driver.manifest = self.manifest
        current_root = Path(self.manifest["artifact_root"])
        result = self.result | {"snapshot_id": self.manifest["snapshot_id"], "head_sha": repository.head}
        result_path = current_root / "runs" / "first" / "result.json"
        files.atomic_json(result_path, result)
        report_path = result_path.with_name("report.md")
        files.atomic_text(report_path, files.render_individual(result, "first", "first"))
        record = {
            "worker_id": "first",
            "model": "first",
            "run_id": "first",
            "result_path": str(result_path),
            "result_sha256": store.digest(result_path),
            "report_path": str(report_path),
            "report_sha256": store.digest(report_path),
        }
        render = files.render_individual
        with patch.object(files, "render_individual", wraps=render) as renderer:
            public, sha, ids, _ = self.driver.report([record])
            self.driver.report([record])
            self.assertEqual(renderer.call_count, 1)
        self.assertEqual(ids, [])
        self.assertEqual(store.digest(public), sha)
        private = public.parent / "private"
        originals = {path.read_bytes() for path in private.glob("original-*")}
        self.assertIn(candidate_path.read_bytes(), originals)
        self.assertIn(original_path.read_bytes(), originals)
        self.assertIn((Path(self.manifest["worktree_path"]) / "source.py").read_bytes(), originals)
        candidate_path.write_text("changed parent round source")
        with self.assertRaisesRegex(ValueError, "source input hash changed"):
            self.driver.report([record])
        self.driver.evaluate.assert_not_called()
