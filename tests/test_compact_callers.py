"""Production producers submit only semantic compact fields."""
from contextlib import closing
import json
from pathlib import Path
import re
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import audit_result, render_prompt, result_contract


class CompactCallerTests(unittest.TestCase):
    def test_all_prompt_examples_are_compact_submissions(self):
        samples = json.loads((Path(__file__).parent / 'fixtures' / 'worker_prompt_inputs.json').read_text())
        for sample in samples:
            with self.subTest(route=sample['name']):
                prompt = render_prompt.render_prompt(sample['task'], sample['route_result'], sample['events'],
                    sample.get('runtime_context'), sample.get('project_memories'), sample.get('runtime_knowledge'))
                submission = json.loads(re.search(r"<<'JSON'\n(.*?)\nJSON", prompt, re.S)[1])
                if submission.get('review_input_kind', '').startswith('<'):
                    submission['review_input_kind'] = 'request'
                result_contract.validate_submission(submission, sample['route_result']['expected_output'],
                    sample.get('runtime_context', {}).get('origin_type', 'github'))
                self.assertNotIn('<!-- robert-', prompt)

    def test_classify_requires_explicit_kind(self):
        result = {'output_type': 'review_comment', 'review_point_evaluation': [
            {'summary': 's', 'verdict': 'incorrect', 'reasoning': 'r', 'action': 'comment'}]}
        self.assertEqual(audit_result._audit_review_point_evaluation(result, ['comment'], 'classify'),
                         ['invalid_review_input_kind'])

    def test_new_pr_recommendation_requires_branch_slug(self):
        self.assertIsNotNone(audit_result._audit_classification_recommendation(
            {'output_type': 'classification_result', 'recommended_route': 'new-pr'}))

    def test_single_issue_still_validates_explicit_target(self):
        envelope = {'task_id': 't', 'attempt_id': 'a', 'output_type': 'new_pr', 'submission': {
            'planned_github_actions': [{'type': 'open_pr', 'body': 'Change', 'title': 'Change',
                'issue_resolution': 'complete', 'target_url': 'https://github.com/other/repo/issues/1'}],
            'consumed_event_fingerprints': [], 'used_skills': [], 'verification': []}}
        settings = {'origin_type': 'github', 'action_scope': {'repo': 'example/backend',
            'workstream_id': 'ws', 'branch_name': 'feature', 'base_branch': 'main',
            'sources': [{'source_type': 'issue', 'number': 1}]}}
        with self.assertRaises(result_contract.InvalidSubmission):
            result_contract.normalize(envelope, settings)


class CutoverTests(unittest.TestCase):
    def test_cutover_is_read_only_and_ignores_terminal_history(self):
        import sqlite3
        from robert_agent import result_cutover
        db = sqlite3.connect(':memory:')
        db.executescript('''CREATE TABLE workstreams(active_task_id TEXT,lifecycle TEXT);
            CREATE TABLE tasks(task_id TEXT,lifecycle TEXT);
            CREATE TABLE attempts(attempt_id TEXT,task_id TEXT,status TEXT,metadata_json TEXT);
            CREATE TABLE worker_results(result_id TEXT,task_id TEXT,metadata_json TEXT);
            CREATE TABLE github_actions(action_id TEXT,result_id TEXT,publish_status TEXT,audit_status TEXT);
            INSERT INTO tasks VALUES('t','running');
            INSERT INTO attempts VALUES('a','t','prepared','{}');''')
        before = db.total_changes
        blocked = result_cutover.inspect(db, '/tmp/nonexistent-cutover-test-db')
        self.assertFalse(blocked['ok'])
        self.assertEqual(blocked['blockers'][0]['kind'], 'legacy_prepared')
        self.assertEqual(db.total_changes, before)
        db.execute("UPDATE tasks SET lifecycle='completed'")
        db.execute("UPDATE attempts SET status='completed'")
        db.execute("INSERT INTO worker_results VALUES('r','t','{}')")
        self.assertTrue(result_cutover.inspect(db, '/tmp/nonexistent-cutover-test-db')['ok'])
        db.execute("INSERT INTO github_actions VALUES('action','r','not_published','accepted')")
        self.assertFalse(result_cutover.inspect(db, '/tmp/nonexistent-cutover-test-db')['ok'])
        db.close()

    def test_direct_publisher_blocks_legacy_push_before_external_effects(self):
        import sqlite3
        from unittest.mock import Mock
        from tests.test_reliability import PublicationBudgetTests
        from robert_agent import publish
        fixture = PublicationBudgetTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute("UPDATE worker_results SET metadata_json=json_remove(metadata_json,'$.result_contract')")
        commands = Mock(side_effect=AssertionError("old protocol must not reach external effects"))
        response = publish.publish_ready_actions(fixture.db, run_command=commands)
        self.assertEqual(response['status'], 'blocked_result_contract_cutover', response)
        self.assertFalse(response['ok'])
        commands.assert_not_called()
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM publication_intents').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT publish_status FROM github_actions').fetchone()[0], 'not_published')

    def test_background_publisher_blocks_cutover_before_notification_delivery(self):
        import sqlite3
        from unittest.mock import patch
        from tests.test_reliability import PublicationBudgetTests
        from robert_agent import github_scan
        fixture = PublicationBudgetTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute("UPDATE worker_results SET metadata_json=json_remove(metadata_json,'$.result_contract')")
        config = {'ok': True, 'db_path': fixture.db, 'repos': [{'full_name': 'x/y', 'github_account': 'bot'}]}
        with patch.object(github_scan.validate_config, 'validate_config', return_value=config), \
             patch('robert_agent.notify.deliver_pending') as notify, \
             patch.object(github_scan, '_budget', return_value=True), \
             patch('robert_agent.publish.publish_ready_actions') as publish:
            response = github_scan.publish('unused')
        self.assertEqual(response['status'], 'blocked_result_contract_cutover', response)
        notify.assert_not_called()
        publish.assert_not_called()

    def test_completed_legacy_result_with_active_workstream_is_not_terminal_history(self):
        import sqlite3
        from tests.test_reliability import PublicationBudgetTests
        from robert_agent import result_cutover
        fixture = PublicationBudgetTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle='completed'")
            conn.execute("UPDATE workstreams SET active_task_id='t'")
            conn.execute("DELETE FROM github_actions")
            conn.execute("UPDATE worker_results SET output_type='classification_result', metadata_json=json_set(json_remove(metadata_json,'$.result_contract'),'$.recommended_route','new-pr')")
        self.assertFalse(result_cutover.check(fixture.db)['ok'])
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute("UPDATE workstreams SET lifecycle='completed', active_task_id=NULL")
        self.assertTrue(result_cutover.check(fixture.db)['ok'])

    def test_pending_legacy_spool_is_preserved(self):
        import sqlite3
        import tempfile
        from robert_agent import result_cutover, result_spool
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'db.sqlite'
            db = sqlite3.connect(path)
            db.executescript('''CREATE TABLE workstreams(active_task_id TEXT,lifecycle TEXT);
            CREATE TABLE tasks(task_id TEXT,lifecycle TEXT);
                CREATE TABLE attempts(attempt_id TEXT,task_id TEXT,status TEXT,metadata_json TEXT);
                CREATE TABLE worker_results(result_id TEXT,task_id TEXT,metadata_json TEXT);
                CREATE TABLE github_actions(action_id TEXT,result_id TEXT,publish_status TEXT,audit_status TEXT);''')
            spool = result_spool.directory(path)
            spool.mkdir()
            record = spool / 'legacy.json'
            record.write_text('{"status":"pending","payload":{}}')
            snapshot = record.read_bytes()
            self.assertFalse(result_cutover.inspect(db, path)['ok'])
            self.assertEqual(record.read_bytes(), snapshot)
            db.close()


class CutoverControlPathTests(unittest.TestCase):
    from tests.test_review_input_workflow import ReviewInputIntegrationTests as Fixture
    setUp = Fixture.setUp
    prepare = Fixture.prepare

    def test_legacy_prepared_blocks_run_but_cancel_and_replacement_are_available(self):
        import sqlite3
        from contextlib import closing
        from robert_agent import result_cutover, run_once, work_items
        db, task, attempt, _, _ = self.prepare('comment')
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json='{}' WHERE attempt_id=?", (attempt,))
            item_id, version, repo_id = conn.execute('''SELECT wi.work_item_id,wi.version,wi.repo_id
                FROM work_items wi JOIN tasks t ON wi.workstream_id=t.workstream_id WHERE t.task_id=?''', (task,)).fetchone()
        self.assertEqual(run_once.run_once(self.config_path, dry_run=True, skip_external=True)['status'],
                         'blocked_result_contract_cutover')
        context = work_items.CommandContext('operator', 'owner', frozenset({repo_id}), frozenset({'default'}))
        work_items.execute_command(db, item_id, context=context, command='cancel',
                                   expected_version=version, idempotency_key='cutover-cancel')
        self.assertTrue(result_cutover.check(db)['ok'])
        created = work_items.create_work_item(db, context=context, repo_id=repo_id,
            title='Replacement task', description='Finish the approved request', priority='P1',
            routing_mode='auto', requested_worker=None, start=True, idempotency_key='cutover-replacement')
        self.assertNotEqual(created['item']['work_item_id'], item_id)
        self.assertTrue(run_once.run_once(self.config_path, dry_run=True, skip_external=True)['ok'])
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute('SELECT status,metadata_json FROM attempts WHERE attempt_id=?',
                (attempt,)).fetchone(), ('canceled', '{}'))
            contracts = [json.loads(row[0]).get('result_contract') for row in conn.execute(
                "SELECT metadata_json FROM attempts WHERE attempt_id!=?", (attempt,))]
        self.assertTrue(contracts)
        self.assertTrue(all(contract == result_contract.CONTRACT_ID for contract in contracts))
