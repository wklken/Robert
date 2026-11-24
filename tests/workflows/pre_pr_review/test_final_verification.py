"""Final verifier reuse follows report contents across checkpoint locations."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from robert_agent.adapters.git.review_revision import git
from robert_agent.execution.stages import StageStopped
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.workflows.pre_pr_review import final_verification
from tests.support.pre_pr_review import PrePRReviewFixture


class FinalVerificationIdentityTests(PrePRReviewFixture):
    def setUp(self):
        super().setUp()
        self.driver = self.start(["claude"])
        self.with_finding = True
        self.driver.prepare()
        self.report, self.report_sha, self.findings, _ = self.driver.report(self.driver.review())
        self.disposition = self.driver.address(self.report, self.report_sha, self.findings)

    def verify(self, report=None):
        return final_verification.verify(
            self.driver, report or self.report, self.report_sha, self.findings, self.disposition
        )

    def move_report(self):
        path = self.report.parent.parent / "new-renderer-version" / self.report.name
        path.parent.mkdir()
        path.write_bytes(self.report.read_bytes())
        return path

    def runs(self):
        with store.connect(self.db) as conn:
            return [run for run in store.runs(conn, self.driver.round_id) if run["stage"] == "verify_final"]

    def refresh_snapshot(self):
        upstream = self.git_fixture.upstream
        git(upstream, "checkout", "main")
        (upstream / "unrelated.py").write_text("upstream_value = True\n")
        git(upstream, "add", "unrelated.py")
        git(upstream, "commit", "-m", "advance upstream")
        base = git(upstream, "rev-parse", "HEAD")
        git(self.driver.repo["repo_root"], "fetch", "upstream", base)
        self.driver.save_candidate_snapshot(self.driver.manifest["candidate"], base)
        self.driver.prepare_context()
        self.report, self.report_sha, self.findings, _ = self.driver.report(self.driver.review())
        self.disposition = self.driver.address(self.report, self.report_sha, self.findings)

    def assert_snapshot_refresh_launches_new_verifier(self, original):
        original_inputs = Path(self.driver.final_review["input_path"])
        original_prompt = Path(original["dispatch"]["prompt_path"])
        frozen = {path: path.read_bytes() for path in (original_inputs, original_prompt)}
        old_snapshot = self.driver.manifest["snapshot_id"]
        self.refresh_snapshot()
        self.assertNotEqual(self.driver.manifest["snapshot_id"], old_snapshot)

        second = self.verify()

        self.assertNotEqual(second["run_id"], original["run_id"])
        self.assertEqual(self.launches["verify_final"], 2)
        self.assertEqual(second["snapshot_id"], self.driver.manifest["snapshot_id"])
        self.assertEqual(self.runs()[0], original)
        for path, content in frozen.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(self.verify(), second)
        self.assertEqual(self.launches["verify_final"], 2)

    def test_snapshot_refresh_preserves_completed_verifier_and_launches_current_one(self):
        self.verify()
        self.assert_snapshot_refresh_launches_new_verifier(self.runs()[0])

    def test_snapshot_refresh_skips_interrupted_preupgrade_verifier_without_checkpoint(self):
        self.legacy_verification()
        original = self.runs()[0]
        dispatch = dict(original["dispatch"])
        dispatch.pop("launch_checkpoint", None)
        dispatch.pop("result_checkpoint", None)
        with store.connect(self.db) as conn:
            store.update_run(conn, original["run_id"], status="interrupted", dispatch_json=json.dumps(dispatch))
        self.assert_snapshot_refresh_launches_new_verifier(self.runs()[0])

    def test_current_verifier_prompt_moved_to_registered_old_snapshot_is_refused(self):
        self.verify()
        old_root = Path(self.driver.manifest["artifact_root"])
        self.refresh_snapshot()
        self.verify()
        current = self.runs()[-1]
        prompt = Path(current["dispatch"]["prompt_path"])
        moved = old_root / prompt.name
        moved.write_bytes(prompt.read_bytes())
        dispatch = {**current["dispatch"], "prompt_path": str(moved)}
        with store.connect(self.db) as conn:
            store.update_run(conn, current["run_id"], dispatch_json=json.dumps(dispatch))
        with self.assertRaisesRegex(StageStopped, "registered final reviewer prompt path changed"):
            self.verify()
        self.assertEqual(self.launches["verify_final"], 2)

    def test_unregistered_prompt_directory_is_refused_even_with_unchanged_bytes(self):
        self.verify()
        run = self.runs()[0]
        prompt = Path(run["dispatch"]["prompt_path"])
        unknown = prompt.parent.parent / "unregistered-snapshot"
        unknown.mkdir()
        moved = unknown / prompt.name
        moved.write_bytes(prompt.read_bytes())
        with store.connect(self.db) as conn:
            store.update_run(
                conn, run["run_id"], dispatch_json=json.dumps({**run["dispatch"], "prompt_path": str(moved)})
            )
        with self.assertRaisesRegex(StageStopped, "registered final reviewer prompt path changed"):
            self.verify()
        self.assertEqual(self.launches["verify_final"], 1)

    def test_identical_report_at_new_checkpoint_reuses_registered_run_and_prompt(self):
        first = self.verify()
        run = self.runs()[0]
        prompt = Path(run["dispatch"]["prompt_path"])
        prompt_bytes = prompt.read_bytes()
        inputs = Path(self.driver.final_review["input_path"])
        input_bytes = inputs.read_bytes()

        self.assertEqual(self.verify(self.move_report()), first)

        self.assertEqual(self.launches["verify_final"], 1)
        reused = self.runs()
        self.assertEqual(len(reused), 1)
        for field in ("run_id", "prompt_sha256", "dispatch", "artifact", "started_at"):
            self.assertEqual(reused[0][field], run[field])
        self.assertEqual(Path(self.driver.final_review["input_path"]), inputs)
        self.assertEqual(inputs.read_bytes(), input_bytes)
        self.assertEqual(prompt.read_bytes(), prompt_bytes)

    def test_identical_report_move_adopts_interrupted_run_without_model_call(self):
        wait = self.driver._wait

        def interrupt(run, launched, timeout):
            if run["stage"] == "verify_final":
                raise KeyboardInterrupt("verifier controller interrupted")
            return wait(run, launched, timeout)

        with patch.object(self.driver, "_wait", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.verify()
        original_run = self.runs()[0]

        artifact = self.verify(self.move_report())

        self.assertEqual(artifact["run_id"], original_run["run_id"])
        self.assertEqual(self.launches["verify_final"], 1)
        self.assertEqual(len(self.runs()), 1)

    def test_report_move_and_renderer_upgrade_keep_registered_prompt(self):
        first = self.verify()
        with patch.object(final_verification, "render_prompt", return_value=SimpleNamespace(text="upgraded renderer")):
            self.assertEqual(self.verify(self.move_report()), first)
        self.assertEqual(self.launches["verify_final"], 1)

    def test_forged_current_report_is_refused_before_verifier_launch(self):
        current = self.move_report()
        current.write_text("Forged: all findings resolved.\n")
        with self.assertRaises(StageStopped):
            self.verify(current)
        self.assertEqual(self.launches["verify_final"], 0)
        self.assertEqual(self.runs(), [])

    def test_forged_current_report_cannot_reuse_completed_verifier(self):
        self.verify()
        current = self.move_report()
        current.write_text("Forged: all findings resolved.\n")
        with self.assertRaises(StageStopped):
            self.verify(current)
        self.assertEqual(self.launches["verify_final"], 1)

    def test_changed_frozen_inputs_refuse_completed_verifier_reuse(self):
        self.verify()
        inputs = Path(self.driver.final_review["input_path"])
        inputs.write_bytes(inputs.read_bytes() + b"\n")
        with self.assertRaises((StageStopped, ValueError)):
            self.verify(self.move_report())
        self.assertEqual(self.launches["verify_final"], 1)

    def test_changed_pinned_report_refuses_completed_verifier_reuse(self):
        self.verify()
        report = Path(self.driver.final_review["report_path"])
        report.write_bytes(report.read_bytes() + b"\n")
        with self.assertRaises((StageStopped, ValueError)):
            self.verify(self.move_report())
        self.assertEqual(self.launches["verify_final"], 1)

    def test_changed_report_sha_requires_an_independent_verifier(self):
        first = self.verify()
        current = self.move_report()
        current.write_bytes(current.read_bytes() + b"\nAdditional reviewer evidence.\n")
        self.report_sha = store.digest(current)
        second = self.verify(current)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(self.launches["verify_final"], 2)

    def test_changed_disposition_evidence_requires_an_independent_verifier(self):
        first = self.verify()
        path = Path(self.disposition["dispositions_path"])
        value = json.loads(path.read_text())
        value["dispositions"][0]["reason"] += " Checked the direct caller as well."
        path.write_text(json.dumps(value))
        self.disposition["dispositions_sha256"] = store.digest(path)
        second = self.verify(self.move_report())
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(self.launches["verify_final"], 2)

    def legacy_verification(self):
        prompt = self.driver.prompt

        def legacy_prompt(name, text, **kwargs):
            if name.startswith("final-review-report-"):
                return self.report
            return prompt(name, text, **kwargs)

        # The preupgrade format uses its complete input JSON hash for the
        # filename/run key. Force only that historical hash choice, leaving
        # the real prompt, launch, output validation and registration intact.
        original_hash = hashlib.sha256

        def legacy_hash(value=b""):
            if value.startswith(b"{\n") and b'"dispositions_path"' in value and b'"report_path"' not in value:
                document = json.loads(value)
                document["report_path"] = str(self.report)
                value = json.dumps(document, sort_keys=True, indent=2).encode()
            return original_hash(value)

        with (
            patch.object(self.driver, "prompt", side_effect=legacy_prompt),
            patch.object(final_verification.hashlib, "sha256", side_effect=legacy_hash),
        ):
            return self.verify()

    def test_path_bound_preupgrade_run_is_reused_with_its_exact_input_digest(self):
        first = self.legacy_verification()
        original = self.runs()[0]
        input_path = Path(self.driver.final_review["input_path"])
        input_bytes = input_path.read_bytes()

        self.assertEqual(self.verify(self.move_report()), first)

        self.assertEqual(self.launches["verify_final"], 1)
        self.assertEqual(self.runs()[0]["run_id"], original["run_id"])
        self.assertEqual(Path(self.driver.final_review["input_path"]), input_path)
        self.assertEqual(input_path.read_bytes(), input_bytes)
        self.assertEqual(first["input_sha256"], store.digest(input_path))

    def test_changed_preupgrade_report_refuses_reuse_of_identical_current_report(self):
        self.legacy_verification()
        current = self.move_report()
        self.report.write_bytes(self.report.read_bytes() + b"\nTampered old checkpoint.\n")
        with self.assertRaises(StageStopped):
            self.verify(current)
        self.assertEqual(self.launches["verify_final"], 1)

    def test_changed_registered_prompt_refuses_completed_verifier_reuse(self):
        self.verify()
        prompt = Path(self.runs()[0]["dispatch"]["prompt_path"])
        prompt.write_bytes(prompt.read_bytes() + b"\nTampered reviewer instructions.\n")
        with self.assertRaises(StageStopped):
            self.verify(self.move_report())
        self.assertEqual(self.launches["verify_final"], 1)

    def test_forged_report_after_launch_is_refused_by_output_validation(self):
        current = self.move_report()
        launch = self.driver.launcher

        def forge_after_launch(**kwargs):
            result = launch(**kwargs)
            current.write_text("Forged while final reviewer ran.\n")
            return result

        with patch.object(self.driver, "launcher", side_effect=forge_after_launch), self.assertRaises(StageStopped):
            self.verify(current)
        self.assertEqual(self.launches["verify_final"], 1)
