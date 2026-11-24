"""Deterministic public reports resume from durable, verified stage receipts."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from robert_agent.security import redaction
from robert_agent.workflows.review import files, report_checkpoint as checkpoint
from tests.support.report_checkpoint import forge_redacted_report
from tests.support.review_coordinator import ReviewCoordinatorFixture


class ReportCheckpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.manifest = {
            "schema_version": 1,
            "round_id": "round-1",
            "snapshot_id": "snapshot-1",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "base_ref": "main",
            "pr_number": 42,
            "artifact_root": str(self.root),
            "files": {},
        }
        result = {key: self.manifest[key] for key in ("schema_version", "round_id", "snapshot_id", "head_sha")}
        result.update(
            findings=[],
            dismissed_findings=[],
            merge_recommendation="ready to merge",
            coverage={"reviewed_paths": ["source.py"], "unreviewed_paths": [], "notes": "Read callers"},
        )
        self.records = []
        for worker in ("first", "second"):
            result_path = self.root / "runs" / worker / "result.json"
            files.atomic_json(result_path, result)
            report = result_path.with_name("report.md")
            files.atomic_text(report, files.render_individual(result, worker, worker))
            self.records.append(
                {
                    "worker_id": worker,
                    "run_id": worker,
                    "model": worker,
                    "result_path": str(result_path),
                    "result_sha256": files.sha256_file(result_path),
                    "report_path": str(report),
                    "report_sha256": files.sha256_file(report),
                }
            )
        self.evaluation = {key: result[key] for key in ("schema_version", "round_id", "snapshot_id", "head_sha")}
        self.evaluation.update(items=[], merge_recommendation="ready to merge", merge_reason="No findings")
        self.evaluation_path = self.root / "synthesis" / "sealed" / "synthesis-1" / "evaluation.json"
        files.atomic_json(self.evaluation_path, self.evaluation)
        source = self.root / "context" / "source.md"
        files.atomic_text(source, "Private source notes remain retained\n")
        self.manifest["files"][str(source)] = files.sha256_file(source)
        self.identity = {
            "records": self.records,
            "evaluation_path": str(self.evaluation_path),
            "evaluation_sha256": files.sha256_file(self.evaluation_path),
        }
        self.render = Mock(side_effect=lambda: files.render_merged(self.manifest, self.records, self.evaluation))
        self.redact = Mock(wraps=redaction.redact_text)
        self.register = Mock()
        self.anchors = {}
        self.load_anchor = lambda input_sha: self.anchors.get(input_sha)

        def save_anchor(report):
            self.anchors.setdefault(
                report["input_sha256"],
                {
                    "path": str(report["checkpoint_path"]),
                    "sha256": report["checkpoint_sha256"],
                },
            )

        self.save_anchor = save_anchor
        self.versions = {
            "renderer_version": checkpoint.code_version(files.__file__),
            "redaction_version": checkpoint.code_version(redaction.__file__),
        }

    def run_report(self, **kwargs):
        options = {
            "redact": self.redact,
            "register": self.register,
            "load_anchor": self.load_anchor,
            "save_anchor": self.save_anchor,
            **self.versions,
        }
        options.update(kwargs)
        return checkpoint.seal_rendered_report(
            self.root / "review.sqlite3",
            "round-1",
            self.manifest,
            self.identity,
            self.render,
            **options,
        )

    def test_registration_failure_retries_only_registration_and_preserves_all_originals(self):
        self.register.side_effect = [OSError("DB registration failed"), None]
        with self.assertRaisesRegex(OSError, "DB registration failed"):
            self.run_report()
        report = self.run_report()
        self.assertEqual((self.render.call_count, self.redact.call_count, self.register.call_count), (1, 1, 2))
        base = report["checkpoint_path"].parent
        originals = list((base / "private").glob("original-*"))
        self.assertGreaterEqual(len(originals), 4)
        retained = {path.read_bytes() for path in originals}
        self.assertIn(self.evaluation_path.read_bytes(), retained)
        self.assertIn(next(iter(self.manifest["files"])).encode(), (base / "private" / "input.json").read_bytes())
        self.assertEqual(files.sha256_file(report["path"]), report["sha256"])
        self.assertEqual(files.sha256_file(report["checkpoint_path"]), report["checkpoint_sha256"])
        self.assertEqual(base.stat().st_mode & 0o777, 0o700)
        self.assertEqual((base / "private").stat().st_mode & 0o777, 0o700)
        self.assertEqual({p.name for p in base.glob("*.json")}, {"render.json", "redaction.json", "seal.json"})
        self.assertEqual(report["evaluation_identity"]["evaluation_sha256"], files.sha256_file(self.evaluation_path))

    def test_coherent_forgery_without_trusted_anchor_cannot_bypass_public_redaction(self):
        self.register.side_effect = OSError("registration failed")
        with self.assertRaises(OSError):
            self.run_report(load_anchor=None, save_anchor=None)
        base = next((self.root / "report-checkpoints").iterdir())
        forge_redacted_report(base)
        self.register.reset_mock()
        self.register.side_effect = None
        with self.assertRaises((ValueError, checkpoint.RedactionBlocked)):
            self.run_report(load_anchor=None, save_anchor=None)
        self.register.assert_not_called()
        self.assertEqual(self.render.call_count, 2)  # No trusted receipt committed; rederive once.

    def test_safe_wrong_findings_coherent_chain_without_anchor_are_refused(self):
        self.run_report(load_anchor=None, save_anchor=None)
        base = next((self.root / "report-checkpoints").iterdir())
        forge_redacted_report(base, b"# All findings resolved; ready to merge\n")
        self.register.reset_mock()
        with self.assertRaisesRegex(ValueError, "derivation"):
            self.run_report()
        self.register.assert_not_called()
        self.assertFalse(self.anchors)

    def test_coherent_forgery_after_trusted_anchor_commit_is_refused_without_rederive(self):
        self.register.side_effect = OSError("report registration unavailable")
        with self.assertRaises(OSError):
            self.run_report()
        base = next((self.root / "report-checkpoints").iterdir())
        self.assertEqual(len(self.anchors), 1)
        forge_redacted_report(base, b"# All findings resolved\n")
        self.register.reset_mock()
        with self.assertRaisesRegex(ValueError, "trusted report seal anchor changed"):
            self.run_report()
        self.register.assert_not_called()
        self.assertEqual((self.render.call_count, self.redact.call_count), (1, 1))

    def test_missing_anchor_cache_rederives_once_then_trusted_retry_reuses(self):
        self.run_report(load_anchor=None, save_anchor=None)
        self.run_report()
        self.assertEqual((self.render.call_count, self.redact.call_count), (2, 2))
        self.run_report()
        self.assertEqual((self.render.call_count, self.redact.call_count), (2, 2))

    def test_renderer_crash_retains_inputs_and_retries_only_renderer(self):
        self.render.side_effect = [RuntimeError("renderer crash"), "# recovered report\n"]
        with self.assertRaisesRegex(RuntimeError, "renderer crash"):
            self.run_report()
        self.assertEqual((self.render.call_count, self.redact.call_count, self.register.call_count), (1, 0, 0))
        pinned = list((self.root / "report-checkpoints").glob("*/private/input.json"))
        self.assertEqual(len(pinned), 1)
        report = self.run_report()
        self.assertEqual(report["path"].read_text(), "# recovered report\n")
        self.assertEqual((self.render.call_count, self.redact.call_count, self.register.call_count), (2, 1, 1))

    def test_redaction_block_retains_private_input_and_retries_policy_after_cause_is_handled(self):
        raw = "# report\npassword=private-example-value\n"
        self.render.side_effect = None
        self.render.return_value = raw
        for _ in range(2):
            with self.assertRaises(checkpoint.RedactionBlocked) as raised:
                self.run_report()
            self.assertFalse(raised.exception.result["ok"])
        self.assertEqual((self.render.call_count, self.redact.call_count, self.register.call_count), (2, 2, 0))
        private = next((self.root / "report-checkpoints").glob("*/private"))
        self.assertEqual((private / "rendered.md").read_text(), raw)
        self.assertTrue(list(private.glob("blocked-*.json")))
        self.assertFalse(list((self.root / "report-checkpoints").glob("*/public.md")))
        # Resolve the policy cause by using the existing stricter report sanitizer;
        # its actual source version selects a new report identity, with no models.
        report = self.run_report(
            redact=redaction.redact_report,
            redaction_version=checkpoint.code_version(redaction.__file__, checkpoint.__file__),
        )
        self.assertNotIn("private-example-value", report["path"].read_text())
        self.assertIn("redacted sensitive report line", report["path"].read_text())
        self.assertFalse(redaction.redact_text(raw)["ok"])

    def test_blocked_redactor_can_retry_unchanged_input_after_external_cause_is_handled(self):
        blocked = {"ok": False, "findings": [{"rule": "unavailable_policy", "line": 1, "column": 1}]}
        self.redact.side_effect = [blocked, {"ok": True, "text": "Safe report", "status": "redacted"}]
        with self.assertRaises(checkpoint.RedactionBlocked):
            self.run_report()
        self.run_report()
        self.assertEqual((self.render.call_count, self.redact.call_count, self.register.call_count), (2, 2, 1))

    def test_owning_policy_is_preserved_for_sensitive_line_and_no_public_registration_occurs(self):
        self.render.side_effect = None
        self.render.return_value = "# review\npassword=private-example-value\n"
        with self.assertRaises(checkpoint.RedactionBlocked):
            self.run_report(redact=redaction.redact_text)
        self.register.assert_not_called()

    def test_tampered_public_private_or_step_checkpoint_is_refused_before_registration(self):
        report = self.run_report()
        base = report["checkpoint_path"].parent
        targets = [
            base / "public.md",
            base / "private" / "rendered.md",
            base / "private" / "redacted.md",
            base / "private" / "input.json",
            *base.glob("*.json"),
            *list((base / "private").glob("original-*"))[:1],
        ]
        for target in targets:
            with self.subTest(target=target.name):
                original = target.read_bytes()
                target.write_bytes(original + b" forged")
                self.register.reset_mock()
                with self.assertRaises(ValueError):
                    self.run_report()
                self.register.assert_not_called()
                target.write_bytes(original)
        self.assertEqual((self.render.call_count, self.redact.call_count), (1, 1))

    def test_tampered_source_or_exact_evaluation_bytes_are_refused_without_recomputing_report(self):
        self.run_report()
        targets = [
            self.evaluation_path,
            Path(self.records[0]["result_path"]),
            Path(self.records[0]["report_path"]),
            Path(next(iter(self.manifest["files"]))),
        ]
        for path in targets:
            with self.subTest(target=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                self.register.reset_mock()
                with self.assertRaisesRegex(ValueError, "source input hash changed"):
                    self.run_report()
                self.register.assert_not_called()
                path.write_bytes(original)
        self.assertEqual((self.render.call_count, self.redact.call_count), (1, 1))

    def test_json_checkpoint_with_recomputed_checksum_cannot_change_input_or_seal_identity(self):
        report = self.run_report()
        targets = [report["checkpoint_path"], report["checkpoint_path"].parent / "private" / "input.json"]
        for path in targets:
            with self.subTest(target=path.name):
                original = path.read_bytes()
                value = json.loads(original)
                if path.name == "seal.json":
                    value["payload"]["input_sha256"] = "0" * 64
                else:
                    value["payload"]["identity"]["manifest"]["head_sha"] = "0" * 40
                raw_payload = json.dumps(
                    value["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
                value["sha256"] = hashlib.sha256(raw_payload).hexdigest()
                path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                self.register.reset_mock()
                with self.assertRaises(ValueError):
                    self.run_report()
                self.register.assert_not_called()
                path.write_bytes(original)

    def test_symlink_checkpoint_or_original_source_is_refused(self):
        report = self.run_report()
        for path in (report["path"], self.evaluation_path):
            with self.subTest(target=path.name):
                original = path.read_bytes()
                destination = self.root / "outside-file"
                destination.write_bytes(original)
                path.unlink()
                path.symlink_to(destination)
                with self.assertRaisesRegex(ValueError, "symlink"):
                    self.run_report()
                path.unlink()
                path.write_bytes(original)

    def test_fsync_save_failure_before_anchor_rederives_renderer_without_models(self):
        original = checkpoint._save
        failed = False

        def interrupted_save(path, payload, root):
            nonlocal failed
            original(path, payload, root)
            if Path(path).name == "render.json" and not failed:
                failed = True
                raise OSError("interrupted after durable render")

        with patch.object(checkpoint, "_save", side_effect=interrupted_save):
            with self.assertRaisesRegex(OSError, "interrupted after durable render"):
                self.run_report()
        self.run_report()
        self.assertEqual((self.render.call_count, self.redact.call_count, self.register.call_count), (2, 1, 1))

    def test_durable_step_before_anchor_commit_rederives_without_model_calls(self):
        for step in ("redaction.json", "seal.json"):
            with self.subTest(step=step):
                original = checkpoint._save
                failed = False

                def interrupted_save(path, payload, root):
                    nonlocal failed
                    original(path, payload, root)
                    if Path(path).name == step and not failed:
                        failed = True
                        raise OSError("interrupted durable step")

                self.versions["renderer_version"] = checkpoint.code_version(files.__file__) + step
                with patch.object(checkpoint, "_save", side_effect=interrupted_save):
                    with self.assertRaisesRegex(OSError, "interrupted durable step"):
                        self.run_report()
                rendered, redacted = self.render.call_count, self.redact.call_count
                self.run_report()
                self.assertEqual((self.render.call_count, self.redact.call_count), (rendered + 1, redacted + 1))

    def test_actual_code_version_change_selects_new_checkpoint_and_keeps_old_provenance(self):
        resource = self.root / "render-resource.txt"
        resource.write_text("renderer v1")
        self.versions["renderer_version"] = checkpoint.code_version(files.__file__, resource)
        first = self.run_report()
        resource.write_text("renderer v2")
        self.versions["renderer_version"] = checkpoint.code_version(files.__file__, resource)
        second = self.run_report()
        self.assertNotEqual(first["input_sha256"], second["input_sha256"])
        self.assertNotEqual(first["path"], second["path"])
        self.assertTrue(first["path"].is_file())
        self.assertEqual((self.render.call_count, self.redact.call_count), (2, 2))

    def test_full_manifest_metadata_changes_select_new_identity(self):
        first = self.run_report()
        self.manifest["worker_config_sha256"] = "d" * 64
        second = self.run_report()
        self.assertNotEqual(first["input_sha256"], second["input_sha256"])
        self.assertTrue(first["checkpoint_path"].exists())

    def test_callback_cannot_mutate_frozen_source_identity_before_commit(self):
        original = deepcopy(self.manifest)

        def mutating_render():
            self.manifest["head_sha"] = "0" * 40
            return "unsafe mutation"

        self.render.side_effect = mutating_render
        with self.assertRaisesRegex(ValueError, "input identity changed"):
            self.run_report()
        self.register.assert_not_called()
        self.manifest.update(original)

    def test_missing_exact_evaluation_or_reviewer_hash_is_refused(self):
        for key in ("evaluation_sha256", "records"):
            with self.subTest(key=key):
                original = deepcopy(self.identity)
                self.identity.pop(key)
                with self.assertRaises(ValueError):
                    self.run_report()
                self.identity = original
        self.render.assert_not_called()

    def test_registration_refusal_is_not_report_success(self):
        for refusal in (False, {"ok": False}):
            with self.subTest(refusal=refusal):
                self.register.return_value = refusal
                with self.assertRaisesRegex(ValueError, "registration refused"):
                    self.run_report()
        self.assertEqual((self.render.call_count, self.redact.call_count), (1, 1))

    def test_unregistered_external_source_is_rejected_even_with_correct_hash(self):
        external = self.root.parent / (self.root.name + "-external.txt")
        self.addCleanup(external.unlink, missing_ok=True)
        external.write_text("Unregistered sibling input")
        self.identity["unregistered"] = {"path": str(external), "sha256": files.sha256_file(external)}
        with self.assertRaisesRegex(ValueError, "outside its pinned provenance"):
            self.run_report()
        self.render.assert_not_called()
        self.register.assert_not_called()

    def test_registered_report_input_loader_validates_all_receipts_without_recomputation(self):
        report = self.run_report()
        identity = checkpoint.report_input_identity(
            self.root / "review.sqlite3",
            "round-1",
            self.manifest,
            report["path"],
            report["sha256"],
            load_anchor=self.load_anchor,
        )
        self.assertEqual(identity, self.identity)
        self.assertEqual((self.render.call_count, self.redact.call_count), (1, 1))
        seal = report["checkpoint_path"]
        seal.unlink()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            checkpoint.report_input_identity(
                self.root / "review.sqlite3",
                "round-1",
                self.manifest,
                report["path"],
                report["sha256"],
                load_anchor=self.load_anchor,
            )

    def test_report_input_loader_rejects_wrong_report_hash_and_corrupt_checkpoint(self):
        report = self.run_report()
        with self.assertRaisesRegex(ValueError, "identity changed"):
            checkpoint.report_input_identity(
                self.root / "review.sqlite3",
                "round-1",
                self.manifest,
                report["path"],
                "0" * 64,
                load_anchor=self.load_anchor,
            )
        marker = report["checkpoint_path"].parent / "redaction.json"
        marker.write_text("{}")
        with self.assertRaises(ValueError):
            checkpoint.report_input_identity(
                self.root / "review.sqlite3",
                "round-1",
                self.manifest,
                report["path"],
                report["sha256"],
                load_anchor=self.load_anchor,
            )


class GenericReportAnchorTests(ReviewCoordinatorFixture):
    def test_public_registration_failure_has_trusted_anchor_and_rejects_coherent_forgery(self):
        from robert_agent.workflows.review import coordinator

        self.cycle([self.event()])
        driver = self.coordinator()
        with patch.object(
            coordinator.db_write_artifacts, "insert_for_synthesize", side_effect=OSError("DB report failed")
        ):
            with self.assertRaisesRegex(OSError, "DB report failed"):
                driver.run()
        calls = len(self.calls)
        public = next(Path(driver.manifest["artifact_root"]).glob("report-checkpoints/*/public.md"))
        with patch.object(coordinator.review_files, "render_merged", side_effect=AssertionError("render repeated")):
            with patch.object(
                coordinator.db_write_artifacts, "insert_for_synthesize", side_effect=OSError("DB report failed")
            ):
                with self.assertRaisesRegex(OSError, "DB report failed"):
                    self.coordinator().run()
        self.assertEqual(len(self.calls), calls)
        forge_redacted_report(public.parent, b"# All findings resolved; ready to merge\n")
        with patch.object(coordinator.db_write_artifacts, "insert_for_synthesize") as register:
            with self.assertRaisesRegex(ValueError, "trusted report seal anchor changed"):
                self.coordinator().run()
            register.assert_not_called()
        self.assertEqual(len(self.calls), calls)
