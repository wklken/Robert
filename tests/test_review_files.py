import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import review_files


class ReviewFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.manifest = {
            'schema_version': 1, 'round_id': 'round-1', 'snapshot_id': 'snapshot-1',
            'head_sha': 'a' * 40, 'base_sha': 'b' * 40, 'base_ref': 'release',
            'merge_base_sha': 'c' * 40, 'artifact_root': str(self.root),
            'context_dir': str(self.root / 'context'), 'worktree_path': str(self.root / 'tree'),
            'pr_number': 42, 'round_no': 1, 'trigger_url': 'https://example.invalid/comment/1',
        }
        self.result = {
            'schema_version': 1, 'round_id': 'round-1', 'snapshot_id': 'snapshot-1',
            'head_sha': 'a' * 40,
            'findings': [{'id': 'F1', 'title': 'Missing guard', 'severity': 'high',
                          'location': {'path': 'src/main.py', 'line': 10},
                          'trigger': 'empty request', 'impact': 'crash',
                          'evidence': 'read without check', 'suggestion': 'check first'}],
            'dismissed_findings': [],
            'coverage': {'reviewed_paths': ['src/main.py'], 'unreviewed_paths': [], 'notes': 'read callers'},
            'merge_recommendation': 'merge after fixes',
        }
        self.path = self.root / 'runs' / 'codex' / 'attempt-1' / 'result.json'
        review_files.atomic_json(self.path, self.result)

    def record(self, worker_id='codex'):
        path = self.path if worker_id == 'codex' else self.root / 'runs' / worker_id / 'attempt-1' / 'result.json'
        review_files.atomic_json(path, self.result)
        report_path = path.with_name('report.md')
        report_path.write_text(review_files.render_individual(self.result, worker_id, worker_id), encoding='utf-8')
        return {'worker_id': worker_id, 'model': worker_id, 'run_id': worker_id + '-1',
                'result_path': str(path), 'result_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'report_path': str(report_path), 'report_sha256': hashlib.sha256(report_path.read_bytes()).hexdigest()}

    def evaluation(self):
        item = {**copy.deepcopy(self.result['findings'][0]), 'disposition': 'confirmed',
                'reason': 'Verified source and exact diff', 'sources': [{'worker_id': 'codex', 'finding_id': 'F1'}]}
        return {key: self.result[key] for key in ('schema_version', 'round_id', 'snapshot_id', 'head_sha')} | {
            'items': [item], 'merge_recommendation': 'merge after fixes', 'merge_reason': '修复崩溃后合并'}

    def test_reads_only_committed_result_and_rejects_wrong_round_and_schema(self):
        self.assertEqual(review_files.read_result(self.path, self.manifest), self.result)
        for mutation in ({'round_id': 'old'}, {'coverage': {}}, {'head_sha': 'b' * 40}):
            review_files.atomic_json(self.path, self.result | mutation)
            with self.assertRaises(ValueError):
                review_files.read_result(self.path, self.manifest)
        self.path.write_text('{', encoding='utf-8')
        with self.assertRaises(ValueError):
            review_files.read_result(self.path, self.manifest)
        self.path.unlink()
        self.path.with_suffix('.json.tmp').write_text(json.dumps(self.result))
        with self.assertRaises(ValueError):
            review_files.read_result(self.path, self.manifest)

    def test_rejects_symlink_and_outside_run_paths(self):
        outside = self.root / 'outside.json'
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
        self.assertEqual(inputs['records'], [record])
        evaluation_path = self.root / 'synthesis' / 'attempt-1' / 'evaluation.json'
        review_files.atomic_json(evaluation_path, self.evaluation())
        self.assertEqual(review_files.read_evaluation(evaluation_path, self.manifest, [record]), self.evaluation())
        missing = self.evaluation() | {'items': []}
        review_files.atomic_json(evaluation_path, missing)
        with self.assertRaises(ValueError):
            review_files.read_evaluation(evaluation_path, self.manifest, [record])
        review_files.atomic_json(evaluation_path, self.evaluation())
        self.path.write_text('{}')
        with self.assertRaises(ValueError):
            review_files.read_evaluation(evaluation_path, self.manifest, [record])

    def test_dynamic_report_and_model_independent_prompt(self):
        records = [self.record(name) for name in ('codex', 'claude', 'third')]
        evaluation = self.evaluation()
        evaluation['items'][0]['sources'] += [
            {'worker_id': name, 'finding_id': 'F1'} for name in ('claude', 'third')]
        report = review_files.render_merged(self.manifest, records, evaluation)
        for heading in ('# PR #42 Code Review 汇总报告', '## Report', '### Critical Issues',
                        '### High Issues', '### Medium Issues', '### Low Issues',
                        '### Dismissed Findings', '### Agent Agreement Summary'):
            self.assertIn(heading, report)
        self.assertIn('| Issue | codex (codex) | claude (claude) | third (third) | Verdict |', report)
        self.assertIn('[from Robert]', report)
        prompt = review_files.build_reviewer_prompt(self.manifest)
        self.assertIn('ROBERT_REVIEW_OUTPUT_DIR', prompt)
        self.assertNotIn(str(self.path.parent), prompt)
        self.assertIn('result.json.tmp', prompt)
        self.assertIn('manifest.json', prompt)

        for record in records:
            record['model'] = 'same-model'
        same_model_report = review_files.render_merged(self.manifest, records, evaluation)
        self.assertIn('多 reviewer review', same_model_report)
        self.assertNotIn('多模型 review', same_model_report)

    def test_reviewer_and_synthesis_prompts_require_adversarial_review(self):
        prompts = {
            'reviewer': review_files.build_reviewer_prompt(self.manifest),
            'synthesis': review_files.build_synthesis_prompt(self.manifest, self.root / 'inputs.json'),
        }
        for phase, prompt in prompts.items():
            with self.subTest(phase=phase):
                self.assertIn('adversarial review', prompt)
                self.assertIn('counterexamples', prompt)
                self.assertIn('evidence', prompt)
        self.assertIn('never add an unsourced finding', prompts['synthesis'])
        self.assertNotIn('adversarial review', review_files.build_prepare_prompt(self.manifest))

    def test_duplicate_or_invented_sources_and_unsafe_locations_are_rejected(self):
        record = self.record()
        path = self.root / 'synthesis' / 'attempt-1' / 'evaluation.json'
        invalid = self.evaluation()
        invalid['items'][0]['sources'][0]['finding_id'] = 'invented'
        review_files.atomic_json(path, invalid)
        with self.assertRaises(ValueError):
            review_files.read_evaluation(path, self.manifest, [record])
        invalid_result = copy.deepcopy(self.result)
        invalid_result['findings'][0]['location']['path'] = '../outside'
        review_files.atomic_json(self.path, invalid_result)
        with self.assertRaises(ValueError):
            review_files.read_result(self.path, self.manifest)
