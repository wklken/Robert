"""Submission snapshots survive replay and constrain final publication audit."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

from tests.support import PACKAGE_ROOT  # noqa: F401
from tests import test_review_input_workflow as integration
from robert_agent import result_contract, result_cutover, result_spool, run_once
from robert_agent.worker import result


class SubmissionHeadTests(unittest.TestCase):
    setUp = integration.ReviewInputIntegrationTests.setUp
    prepare = integration.ReviewInputIntegrationTests.prepare

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.tree, check=True, capture_output=True, text=True).stdout.strip()

    def fixture(self):
        db, task, attempt, _, _ = self.prepare('comment')
        self.tree = self.data_dir / 'submission-tree'
        self.tree.mkdir()
        self.git('init', '-b', 'codex/fix')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        self.git('commit', '--allow-empty', '-m', 'first')
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute('UPDATE attempts SET worktree_path=?,branch_name=? WHERE attempt_id=?', (str(self.tree), 'codex/fix', attempt))
        envelope = result.build_result(task, attempt, 'update_existing_pr', ['comment:request-1'],
            [{'command': 'true', 'status': 'passed', 'purpose': 'regression', 'required': True,
              'exit_code': 0, 'source': 'worker_reported'}], 'Done',
            planned_github_actions=[{'type': 'push_existing_pr'}, {'type': 'comment', 'body': 'Done'}],
            review_input_kind='request')
        return db, task, attempt, envelope

    def metadata(self, db):
        with closing(sqlite3.connect(db)) as conn:
            return json.loads(conn.execute('SELECT metadata_json FROM worker_results').fetchone()[0])

    def test_acknowledged_head_change_is_rejected_by_final_audit(self):
        db, _, _, envelope = self.fixture()
        head = self.git('rev-parse', 'HEAD')
        self.assertTrue(result.record_result(db, envelope)['ok'])
        self.assertEqual(self.metadata(db)['submission_head_sha'], head)
        self.git('commit', '--allow-empty', '-m', 'after acknowledgement')
        run_once.run_once(self.config_path, dry_run=True, skip_external=True)
        self.assertEqual(self.metadata(db)['audit']['violations'], ['submission_head_changed'])

    def test_stopped_process_with_same_head_passes_and_live_process_waits(self):
        db, _, attempt, envelope = self.fixture()
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=json_set(metadata_json,'$.dispatch.pid',123456789) WHERE attempt_id=?", (attempt,))
        self.assertTrue(result.record_result(db, envelope)['ok'])
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE attempts SET status='completed' WHERE attempt_id=?", (attempt,))
        with patch.object(run_once, "_supervise_running_attempts", return_value=0):
            run_once.run_once(self.config_path, dry_run=True, skip_external=True)
        self.assertNotIn('audit', self.metadata(db))
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=json_set(metadata_json,'$.process_stopped_at','2026-10-29T00:00:00+00:00') WHERE attempt_id=?", (attempt,))
        run_once.run_once(self.config_path, dry_run=True, skip_external=True)
        self.assertEqual(self.metadata(db)['audit']['status'], 'accepted')

    def test_replay_and_duplicate_keep_first_observed_head(self):
        db, _, _, envelope = self.fixture()
        head = self.git('rev-parse', 'HEAD')
        with patch.object(result, '_record_result_db', side_effect=sqlite3.OperationalError('database is locked')):
            self.assertEqual(result.record_result(db, envelope)['status'], 'deferred')
        self.git('commit', '--allow-empty', '-m', 'after durable spool')
        with patch.object(result_contract, 'normalize', side_effect=AssertionError('must reuse snapshot')):
            self.assertEqual(result.replay_pending_results(db)['recorded'], 1)
            self.assertTrue(result.record_result(db, envelope)['deduplicated'])
        self.assertEqual(self.metadata(db)['submission_head_sha'], head)
        run_once.run_once(self.config_path, dry_run=True, skip_external=True)
        self.assertEqual(self.metadata(db)['audit']['violations'], ['submission_head_changed'])

    def test_git_read_failure_is_validation_unavailable(self):
        db, _, _, envelope = self.fixture()
        with patch('robert_agent.action_scope.subprocess.run', side_effect=OSError('unavailable')):
            receipt = result.record_result(db, envelope)
        self.assertEqual(receipt['status'], 'validation_unavailable')
        self.assertEqual(list(result_spool.directory(db).glob('*.json')), [])


class CutoverRaceTests(unittest.TestCase):
    def test_ingested_spool_disappearing_after_glob_does_not_block(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.executescript('''CREATE TABLE workstreams(active_task_id TEXT,lifecycle TEXT);
            CREATE TABLE tasks(task_id TEXT,lifecycle TEXT);
            CREATE TABLE attempts(attempt_id TEXT,task_id TEXT,status TEXT,metadata_json TEXT);
            CREATE TABLE worker_results(result_id TEXT,task_id TEXT,metadata_json TEXT);
            CREATE TABLE github_actions(result_id TEXT,publish_status TEXT,audit_status TEXT);''')
        path = Path('/spool/already-ingested.json')
        with patch.object(Path, 'glob', side_effect=[[path], []]), patch.object(Path, 'read_text', side_effect=FileNotFoundError()):
            self.assertTrue(result_cutover.inspect(conn, '/tmp/db')['ok'])
        with patch.object(Path, 'glob', side_effect=[[path], []]), patch.object(Path, 'read_text', side_effect=PermissionError()):
            self.assertFalse(result_cutover.inspect(conn, '/tmp/db')['ok'])


class CompactShapeTests(unittest.TestCase):
    def submission(self):
        return {'planned_github_actions': [], 'used_skills': [], 'verification': [],
                'consumed_event_fingerprints': ['comment:1']}

    def test_json_types_report_the_actual_field(self):
        cases = [({'planned_github_actions': [{'type': []}]}, 'planned_github_actions[0].type'),
                 ({'review_input_kind': {}}, 'review_input_kind')]
        for field in ('status', 'source'):
            entry = {'command': 'true', 'status': 'passed', 'purpose': 'check', 'required': True,
                     'source': 'worker_reported', 'exit_code': 0}
            entry[field] = []
            cases.append(({'verification': [entry]}, 'verification[0].' + field))
        for change, expected in cases:
            with self.subTest(change=change), self.assertRaises(result_contract.InvalidSubmission) as raised:
                result_contract.validate_submission(self.submission() | change, 'local_result', 'github')
            self.assertEqual(raised.exception.field, expected)

    def test_schema_semantic_shapes_are_enforced(self):
        for change in ({'memory_delta': []}, {'memory_delta': {'entries': ['bad']}},
                       {'operator_question': {'kind': 'clarification', 'summary': 'Choose', 'secret': 'bad'}},
                       {'review_point_evaluation': [{'summary': 's', 'verdict': 'incorrect', 'reasoning': 'r', 'action': 'implement'}]},
                       {'review_point_evaluation': [{'summary': 's', 'verdict': 'made_up', 'reasoning': 'r', 'action': 'skip'}]}):
            with self.subTest(change=change), self.assertRaises(result_contract.InvalidSubmission):
                result_contract.validate_submission(self.submission() | change, 'local_result', 'github')
