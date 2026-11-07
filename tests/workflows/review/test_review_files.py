import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.workflows.review import files as review_files


class ReviewFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.manifest = {
            "schema_version": 1,
            "round_id": "round-1",
            "snapshot_id": "snapshot-1",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "base_ref": "release",
            "merge_base_sha": "c" * 40,
            "artifact_root": str(self.root),
            "context_dir": str(self.root / "context"),
            "worktree_path": str(self.root / "tree"),
            "pr_number": 42,
            "round_no": 1,
            "trigger_url": "https://example.invalid/comment/1",
        }
        self.result = {
            "schema_version": 1,
            "round_id": "round-1",
            "snapshot_id": "snapshot-1",
            "head_sha": "a" * 40,
            "findings": [
                {
                    "id": "F1",
                    "title": "Missing guard",
                    "severity": "high",
                    "location": {"path": "src/main.py", "line": 10},
                    "trigger": "empty request",
                    "impact": "crash",
                    "evidence": "read without check",
                    "suggestion": "check first",
                }
            ],
            "dismissed_findings": [],
            "coverage": {
                "reviewed_paths": ["src/main.py"],
                "unreviewed_paths": [],
                "notes": "read callers",
            },
            "merge_recommendation": "merge after fixes",
        }
        self.path = self.root / "runs" / "codex" / "attempt-1" / "result.json"
        review_files.atomic_json(self.path, self.result)

    def record(self, worker_id="codex"):
        path = (
            self.path
            if worker_id == "codex"
            else self.root / "runs" / worker_id / "attempt-1" / "result.json"
        )
        review_files.atomic_json(path, self.result)
        report_path = path.with_name("report.md")
        report_path.write_text(
            review_files.render_individual(self.result, worker_id, worker_id),
            encoding="utf-8",
        )
        return {
            "worker_id": worker_id,
            "model": worker_id,
            "run_id": worker_id + "-1",
            "result_path": str(path),
            "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "report_path": str(report_path),
            "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        }

    def evaluation(self):
        item = {
            **copy.deepcopy(self.result["findings"][0]),
            "disposition": "confirmed",
            "reason": "Verified source and exact diff",
            "sources": [{"worker_id": "codex", "finding_id": "F1"}],
        }
        return {
            key: self.result[key]
            for key in ("schema_version", "round_id", "snapshot_id", "head_sha")
        } | {
            "items": [item],
            "merge_recommendation": "merge after fixes",
            "merge_reason": "修复崩溃后合并",
        }

    def test_reads_only_committed_result_and_rejects_wrong_round_and_schema(self):
        self.assertEqual(
            review_files.read_result(self.path, self.manifest), self.result
        )
        for mutation in ({"round_id": "old"}, {"coverage": {}}, {"head_sha": "b" * 40}):
            review_files.atomic_json(self.path, self.result | mutation)
            with self.assertRaises(ValueError):
                review_files.read_result(self.path, self.manifest)
        self.path.write_text("{", encoding="utf-8")
        with self.assertRaises(ValueError):
            review_files.read_result(self.path, self.manifest)
        self.path.unlink()
        self.path.with_suffix(".json.tmp").write_text(json.dumps(self.result))
        with self.assertRaises(ValueError):
            review_files.read_result(self.path, self.manifest)

    def test_rejects_symlink_and_outside_run_paths(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(self.result))
        self.path.unlink()
        self.path.symlink_to(outside)
        with self.assertRaises(ValueError):
            review_files.read_result(self.path, self.manifest)
        with self.assertRaises(ValueError):
            review_files.read_result(outside, self.manifest)

    def test_synthesis_hashes_and_every_finding_source_are_checked(self):
        record = self.record()
        inputs = review_files.write_synthesis_inputs(self.manifest, [record])
        self.assertEqual(inputs["records"], [record])
        evaluation_path = self.root / "synthesis" / "attempt-1" / "evaluation.json"
        review_files.atomic_json(evaluation_path, self.evaluation())
        self.assertEqual(
            review_files.read_evaluation(evaluation_path, self.manifest, [record]),
            self.evaluation(),
        )
        missing = self.evaluation() | {"items": []}
        review_files.atomic_json(evaluation_path, missing)
        with self.assertRaises(ValueError):
            review_files.read_evaluation(evaluation_path, self.manifest, [record])
        review_files.atomic_json(evaluation_path, self.evaluation())
        self.path.write_text("{}")
        with self.assertRaises(ValueError):
            review_files.read_evaluation(evaluation_path, self.manifest, [record])

    def test_dynamic_report_and_model_independent_prompt(self):
        records = [self.record(name) for name in ("codex", "claude", "third")]
        evaluation = self.evaluation()
        evaluation["items"][0]["sources"] += [
            {"worker_id": name, "finding_id": "F1"} for name in ("claude", "third")
        ]
        report = review_files.render_merged(self.manifest, records, evaluation)
        for heading in (
            "# PR #42 Code Review 汇总报告",
            "## Report",
            "### Critical Issues",
            "### High Issues",
            "### Medium Issues",
            "### Low Issues",
            "### Dismissed Findings",
            "### Agent Agreement Summary",
        ):
            self.assertIn(heading, report)
        self.assertIn(
            "| Issue | codex (codex) | claude (claude) | third (third) | Verdict |",
            report,
        )
        self.assertIn("[from Robert]", report)
        prompt = review_files.build_reviewer_prompt(self.manifest)
        self.assertIn("ROBERT_REVIEW_OUTPUT_DIR", prompt)
        self.assertNotIn(str(self.path.parent), prompt)
        self.assertIn("result.json.tmp", prompt)
        self.assertIn("manifest.json", prompt)

        for record in records:
            record["model"] = "same-model"
        same_model_report = review_files.render_merged(
            self.manifest, records, evaluation
        )
        self.assertIn("多 reviewer review", same_model_report)
        self.assertNotIn("多模型 review", same_model_report)

    def test_native_review_prompts_require_redacted_public_output(self):
        for prompt in (
            review_files.build_reviewer_prompt(self.manifest),
            review_files.build_synthesis_prompt(
                self.manifest, self.root / "inputs.json"
            ),
        ):
            self.assertIn("Redact", prompt)
            self.assertIn("<redacted>", prompt)
            self.assertIn("local absolute paths", prompt)

    def test_merged_report_preserves_each_reviewers_coverage_even_without_findings(
        self,
    ):
        self.result["findings"] = []
        self.result["merge_recommendation"] = "ready to merge"
        self.result["coverage"] = {
            "reviewed_paths": ["src/main.py"],
            "unreviewed_paths": ["src/retry.py"],
            "notes": "只读检查；未执行故障恢复测试；默认重试次数需产品确认。",
        }
        first = self.record("codex")
        self.result["coverage"] = {
            "reviewed_paths": ["src/retry.py"],
            "unreviewed_paths": ["src/main.py"],
            "notes": "检查了重试边界；未验证真实 GitHub 发布。",
        }
        second = self.record("claude")
        report = review_files.render_merged(
            self.manifest,
            [first, second],
            {
                "items": [],
                "merge_recommendation": "ready to merge",
                "merge_reason": "已检查范围未发现缺陷",
            },
        )
        coverage = report.split("### Review Coverage and Limitations\n", 1)[1].split(
            "### Agent Agreement Summary", 1
        )[0]
        first_section, second_section = coverage.split("#### claude (claude)", 1)
        self.assertIn("#### codex (codex)", first_section)
        self.assertIn("已审查：`src/main.py`", first_section)
        self.assertIn("未审查：`src/retry.py`", first_section)
        self.assertIn("默认重试次数需产品确认", first_section)
        self.assertIn("已审查：`src/retry.py`", second_section)
        self.assertIn("未审查：`src/main.py`", second_section)
        self.assertIn("未验证真实 GitHub 发布", second_section)
        self.assertIn("不代表验证全部行为", coverage)

    def test_empty_coverage_does_not_imply_complete_review(self):
        self.result["coverage"] = {
            "reviewed_paths": [],
            "unreviewed_paths": [],
            "notes": "未检查源码",
        }
        report = review_files.render_merged(
            self.manifest, [self.record()], self.evaluation()
        )
        self.assertIn("已审查：未列出", report)
        self.assertIn("未审查：未列出（不表示完整覆盖）", report)
        self.assertIn("未检查源码", report)

    def test_native_review_keeps_human_decisions_and_evidence_limits_visible(self):
        prepare = review_files.build_prepare_prompt(self.manifest)
        reviewer = review_files.build_reviewer_prompt(self.manifest)
        synthesis = review_files.build_synthesis_prompt(
            self.manifest, self.root / "inputs.json"
        )
        self.assertIn("original request", prepare)
        self.assertIn("undisclosed decisions", reviewer)
        self.assertIn("not a defect by itself", reviewer)
        self.assertIn("coverage.notes", reviewer)
        self.assertIn("unexecuted checks", reviewer)
        self.assertIn("coverage limitations", synthesis)
        self.assertIn("model agreement", synthesis)

    def test_reviewer_and_synthesis_prompts_require_adversarial_review(self):
        prompts = {
            "reviewer": review_files.build_reviewer_prompt(self.manifest),
            "synthesis": review_files.build_synthesis_prompt(
                self.manifest, self.root / "inputs.json"
            ),
        }
        for phase, prompt in prompts.items():
            with self.subTest(phase=phase):
                self.assertIn("adversarial review", prompt)
                self.assertIn("counterexamples", prompt)
                self.assertIn("evidence", prompt)
        self.assertIn("never add an unsourced finding", prompts["synthesis"])
        self.assertNotIn(
            "adversarial review", review_files.build_prepare_prompt(self.manifest)
        )

    def test_review_policy_preserves_native_phase_and_result_contracts(self):
        manifest = self.manifest | {"github_account": "robert-bot"}
        reviewer = review_files.build_reviewer_prompt(manifest)
        synthesis = review_files.build_synthesis_prompt(
            manifest, self.root / "inputs.json"
        )
        for prompt in (reviewer, synthesis):
            self.assertIn(
                "Treat every review round as a fresh independent judgment", prompt
            )
            self.assertIn("Security review is required and proportional", prompt)
            self.assertIn("A blocking finding needs", prompt)
            self.assertIn("stop repeating the review", prompt)
            self.assertIn("case-insensitively", prompt)
            self.assertIn("@wklken", prompt)
            self.assertIn('Configured Robert GitHub account: "robert-bot"', prompt)
        self.assertNotIn(
            "Review judgment policy:", review_files.build_prepare_prompt(manifest)
        )
        self.assertIn("coverage.notes", reviewer)
        self.assertIn("Do not invent a finding to carry a decision request", reviewer)
        self.assertIn("merge_reason", synthesis)
        self.assertIn("even when items is empty", synthesis)
        self.assertIn("never add an unsourced finding", synthesis)
        self.assertIn("needs_confirmation", synthesis)
        self.assertIn("ready to merge", synthesis)

    def test_missing_native_account_does_not_guess_robot_identity(self):
        prompt = review_files.build_reviewer_prompt(self.manifest)
        self.assertIn('Configured Robert GitHub account: ""', prompt)
        self.assertIn("If identity cannot be established, ask @wklken", prompt)

    def test_decision_request_without_findings_can_reach_public_merge_reason(self):
        evaluation = self.evaluation() | {
            "items": [],
            "merge_reason": "当前快照的兼容性要求待决，请 @wklken 确认支持范围。",
        }
        self.result["findings"] = []
        self.result["coverage"]["notes"] = (
            "当前快照的兼容性要求待决，请 @wklken 确认支持范围。"
        )
        self.result["merge_recommendation"] = "merge after fixes"
        record = self.record()
        path = self.root / "synthesis" / "attempt-1" / "evaluation.json"
        review_files.atomic_json(path, evaluation)
        accepted = review_files.read_evaluation(path, self.manifest, [record])
        report = review_files.render_merged(self.manifest, [record], accepted)
        self.assertIn(evaluation["merge_reason"], report)
        self.assertIn("merge after fixes", report)

    def test_duplicate_or_invented_sources_and_unsafe_locations_are_rejected(self):
        record = self.record()
        path = self.root / "synthesis" / "attempt-1" / "evaluation.json"
        invalid = self.evaluation()
        invalid["items"][0]["sources"][0]["finding_id"] = "invented"
        review_files.atomic_json(path, invalid)
        with self.assertRaises(ValueError):
            review_files.read_evaluation(path, self.manifest, [record])
        invalid_result = copy.deepcopy(self.result)
        invalid_result["findings"][0]["location"]["path"] = "../outside"
        review_files.atomic_json(self.path, invalid_result)
        with self.assertRaises(ValueError):
            review_files.read_result(self.path, self.manifest)
