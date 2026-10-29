from collections import Counter
from contextlib import closing
import json
import os
import re
from pathlib import Path
import shlex
import sqlite3
import signal
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from tests import test_review_workspace as workspace_fixture
from robert_agent import dispatch, loop_engine, publish, review_coordinator, review_files, review_store, run_once, validate_config
from robert_agent.init_config import init_config
from robert_agent.resource_files import resource


class ReviewCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.git_fixture = workspace_fixture.ReviewWorkspaceTests()
        self.git_fixture.setUp()
        self.addCleanup(self.git_fixture.doCleanups)
        self.root = self.git_fixture.root
        self.config_path = self.root / 'config.yml'
        init_config(self.config_path, {'repo': 'example/backend', 'repo_path': str(self.git_fixture.repo),
                    'worker': 'codex', 'github_account': 'robert-bot', 'trusted_actor': 'maintainer'}, non_interactive=True)
        config = yaml.safe_load(self.config_path.read_text())
        config['data_dir'] = str(self.root / 'data')
        worker = next(iter(config['workers'].values()))
        config['workers'] = {'codex': worker, 'claude': {**worker, 'adapter': 'claude', 'command': 'claude'}}
        config['routes'] = {'review-pr': {'worker': 'codex', 'multi_agent_review': {
            'enabled': True, 'workers': ['codex', 'claude'], 'author_allowlist': ['author']}}}
        self.config_path.write_text(yaml.safe_dump(config, sort_keys=False))
        self.db = self.root / 'data' / 'robert.sqlite3'
        self.calls = []
        self.fail_worker = None
        self.stdout_only = False
        self.bad_round = False

    def event(self, identifier='first', author='author', actor='maintainer'):
        return {'id': identifier, 'number': 42, 'source_type': 'pull_request', 'event_type': 'comment',
                'actor_login': actor, 'pr_author_login': author, 'body': '@robert-bot review this PR',
                'intent': 'review_request', 'title': 'Review example', 'state': 'open',
                'html_url': 'https://github.com/example/backend/pull/42'}

    def cycle(self, events, skip_publish=True):
        fixture = self.root / 'fixture.json'
        fixture.write_text(json.dumps({'events': events}))
        result = run_once.run_once(self.config_path, workflow_path=resource('workflow.yml'),
                                  fixture_path=fixture, dry_run=True, skip_external=True, skip_publish=skip_publish)
        self.assertTrue(result['ok'], result)
        return result

    def current(self):
        with review_store.connect(self.db) as conn:
            task, attempt = conn.execute('''SELECT t.task_id,a.attempt_id FROM tasks t
                JOIN attempts a ON a.task_id=t.task_id ORDER BY t.created_at DESC,a.attempt_no DESC LIMIT 1''').fetchone()
        return task, attempt

    def pr(self, _repo, _number):
        return {'number': 42, 'state': 'open', 'user': {'login': 'author'},
                'head': {'sha': self.git_fixture.head, 'ref': 'feature'},
                'base': {'sha': self.git_fixture.base, 'ref': 'main', 'repo': {'full_name': 'example/backend'}}}

    def launcher(self, **kwargs):
        output = Path(kwargs['trusted_environment']['ROBERT_REVIEW_OUTPUT_DIR'])
        prompt = Path(kwargs['prompt_path'])
        stage = 'review' if prompt.name == 'reviewer-prompt.md' else 'prepare' if prompt.name == 'prepare-prompt.md' else 'synthesis'
        worker = kwargs['worker_agent_config']['name']
        self.calls.append((stage, worker, prompt.read_bytes(), kwargs['worktree_path'], str(output), kwargs['model']))
        manifest = json.loads((prompt.parent / 'context' / 'manifest.json').read_text())
        if worker == self.fail_worker and stage == 'review':
            return {'ok': False, 'safe_error': 'test model unavailable'}
        if stage == 'prepare':
            (output / 'context-summary.md').write_text('Read source.py; integer assignment change. Test entrypoints inspected.')
            review_files.atomic_json(output / 'source-index.json', {'files': [
                {'path': 'source.py', 'reason': 'changed file'}], 'reading_notes': 'Read all changed source'})
        elif stage == 'review':
            if not self.stdout_only:
                payload = {key: manifest[key] for key in ('round_id', 'snapshot_id', 'head_sha')}
                payload.update(schema_version=1, findings=[], dismissed_findings=[],
                               coverage={'reviewed_paths': ['source.py'], 'unreviewed_paths': [], 'notes': 'Read full source'},
                               merge_recommendation='ready to merge')
                if self.bad_round:
                    payload['round_id'] = 'previous-round'
                review_files.atomic_json(output / 'result.json', payload)
        else:
            inputs_path = re.search(r'Read the frozen selected artifact list (.*?)\. Read each', prompt.read_text()).group(1)
            inputs = json.loads(Path(inputs_path).read_text())
            for record in inputs['records']:
                self.assertTrue(Path(record['result_path']).is_file())
            payload = {key: manifest[key] for key in ('round_id', 'snapshot_id', 'head_sha')}
            payload.update(schema_version=1, items=[], merge_recommendation='ready to merge', merge_reason='没有已确认问题')
            review_files.atomic_json(output / 'evaluation.json', payload)
        return {'ok': True, 'process_exited': True, 'returncode': 0, 'pid': 1073741824}

    def coordinator(self):
        task, attempt = self.current()
        return review_coordinator.Coordinator(self.db, task, attempt, fetch_pr=self.pr, launcher=self.launcher)

    def finish_task(self, task):
        with review_store.connect(self.db) as conn:
            conn.execute("UPDATE tasks SET lifecycle='completed' WHERE task_id=?", (task,))
            conn.execute("UPDATE attempts SET status='completed' WHERE task_id=?", (task,))
            conn.execute("UPDATE workstreams SET lifecycle='completed',active_task_id=NULL WHERE active_task_id=?", (task,))
            conn.execute("UPDATE execution_subjects SET binding_status='terminal' WHERE subject_id=?", (task,))

    def test_file_handoff_same_prompt_and_two_independent_rounds(self):
        self.cycle([self.event()])
        first = self.coordinator()
        report1 = first.run()
        self.assertTrue(report1.is_file())
        reviewer_calls = [call for call in self.calls if call[0] == 'review']
        self.assertEqual(reviewer_calls[0][2:4], reviewer_calls[1][2:4])
        self.assertNotEqual(reviewer_calls[0][4], reviewer_calls[1][4])
        self.assertIn('Code Review 汇总报告', report1.read_text())
        self.cycle([self.event('second'), self.event('second')])
        with review_store.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_sessions').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM task_events WHERE relationship='pending'").fetchone()[0], 0)
        self.finish_task(first.task_id)
        self.git_fixture.commit('value = 3\n')
        self.cycle([])  # local drain starts the already queued request, no new GitHub event
        second = self.coordinator()
        report2 = second.run()
        self.assertNotEqual(first.round_id, second.round_id)
        self.assertNotEqual(first.manifest['worktree_path'], second.manifest['worktree_path'])
        self.assertNotEqual(first.manifest['head_sha'], second.manifest['head_sha'])
        self.assertNotEqual(report1, report2)
        self.assertIn('value = 2', Path(first.manifest['worktree_path'], 'source.py').read_text())

    def test_failed_model_retry_reuses_other_result_and_exact_prompt(self):
        self.cycle([self.event()])
        self.fail_worker = 'claude'
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            self.coordinator().run()
        self.fail_worker = None
        self.coordinator().run()
        counts = Counter((call[0], call[1]) for call in self.calls)
        self.assertEqual(counts[('prepare', 'codex')], 1)
        self.assertEqual(counts[('review', 'codex')], 1)
        self.assertEqual(counts[('review', 'claude')], 2)
        self.assertEqual(counts[('synthesis', 'codex')], 1)
        prompts = [call[2] for call in self.calls if call[0] == 'review']
        self.assertEqual(len(set(prompts)), 1)

    def test_advanced_target_branch_completes_review_at_captured_pr_base(self):
        self.cycle([self.event()])
        self.git_fixture.git(self.git_fixture.upstream, 'checkout', 'main')
        (self.git_fixture.upstream / 'other.py').write_text('unrelated = True\n')
        self.git_fixture.git(self.git_fixture.upstream, 'add', '.')
        self.git_fixture.git(self.git_fixture.upstream, 'commit', '-m', 'advance target branch')

        driver = self.coordinator()
        report = driver.run()

        self.assertTrue(report.is_file())
        self.assertEqual(driver.manifest['base_sha'], self.git_fixture.base)
        self.assertTrue(review_coordinator.same_revision(driver.manifest, self.pr('', 42)))
        with review_store.connect(self.db) as conn:
            self.assertEqual(review_store.session(conn, round_id=driver.round_id)['status'], 'ready')

    def test_timeout_fallback_is_per_reviewer_and_survives_file_recovery(self):
        config = yaml.safe_load(self.config_path.read_text())
        config['workers']['claude']['model'] = 'primary-model'
        config['workers']['claude']['fallback_model'] = 'fallback-model'
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([self.event()])
        original_wait = review_coordinator.Coordinator._wait

        def timeout(driver, run, launched, seconds):
            if run['stage'] == 'review' and run['worker_id'] == 'claude':
                raise RuntimeError('review worker timed out')
            return original_wait(driver, run, launched, seconds)

        with patch.object(review_coordinator.Coordinator, '_wait', timeout):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, 'timed out'):
                    self.coordinator().run()
        driver = self.coordinator()
        original_register = driver.register

        def crash(run, artifact):
            if run['stage'] == 'review' and run['worker_id'] == 'claude':
                raise KeyboardInterrupt('fallback file before registration')
            return original_register(run, artifact)

        with patch.object(driver, 'register', side_effect=crash), self.assertRaises(KeyboardInterrupt):
            driver.run()
        report = self.coordinator().run()
        reviews = [call for call in self.calls if call[0] == 'review']
        self.assertEqual([call[5] for call in reviews if call[1] == 'claude'],
                         ['primary-model', 'primary-model', 'fallback-model'])
        self.assertEqual(sum(call[1] == 'codex' for call in reviews), 1)
        self.assertEqual(len({call[2] for call in reviews}), 1)
        self.assertIn('fallback-model', report.read_text())
        with review_store.connect(self.db) as conn:
            selected = [run for run in review_store.runs(conn, driver.round_id)
                        if run['stage'] == 'review' and run['status'] == 'succeeded']
            fallback = next(run for run in selected if run['worker_id'] == 'claude')
            self.assertEqual(fallback['model'], 'fallback-model')
            self.assertEqual(fallback['artifact']['model'], 'fallback-model')
            self.assertIn('fallback-model', json.dumps(review_store.board_status(conn, driver.task_id)))

    def test_rate_limit_automatically_switches_only_affected_reviewer(self):
        config = yaml.safe_load(self.config_path.read_text())
        config['workers']['claude'].update(model='primary-model', fallback_model='fallback-model')
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([self.event()])
        driver = self.coordinator()
        original = driver.launcher

        def limited(**kwargs):
            if kwargs['worker_agent_config']['name'] == 'claude' and kwargs['model'] == 'primary-model':
                output = Path(kwargs['log_dir'])
                log = output / 'stdout.log'
                log.write_text(json.dumps({'type': 'turn.failed', 'error': {'message': '429 Too Many Requests'}}) + '\n')
                self.calls.append(('review', 'claude', Path(kwargs['prompt_path']).read_bytes(),
                                   kwargs['worktree_path'], str(output), kwargs['model']))
                return {'ok': False, 'pid': 1073741824, 'process_exited': True,
                        'returncode': 1, 'stdout_path': str(log), 'safe_error': 'worker exited with code 1'}
            return original(**kwargs)

        driver.launcher = limited
        report = driver.run()
        reviews = [call for call in self.calls if call[0] == 'review']
        self.assertEqual([call[5] for call in reviews if call[1] == 'claude'], ['primary-model', 'fallback-model'])
        self.assertEqual(sum(call[1] == 'codex' for call in reviews), 1)
        self.assertEqual(len({call[2] for call in reviews}), 1)
        self.assertIn('fallback-model', report.read_text())

    def test_rate_limited_fallback_stops_and_survives_coordinator_restart(self):
        config = yaml.safe_load(self.config_path.read_text())
        config['workers']['claude'].update(model='primary-model', fallback_model='fallback-model')
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([self.event()])
        driver = self.coordinator()
        original = driver.launcher
        limited_models = []

        def limited(**kwargs):
            if kwargs['worker_agent_config']['name'] == 'claude':
                limited_models.append(kwargs['model'])
                log = Path(kwargs['log_dir']) / 'stderr.log'
                log.write_text('HTTP 429 Too Many Requests')
                return {'ok': False, 'pid': 1073741824, 'process_exited': True,
                        'returncode': 1, 'stderr_path': str(log), 'safe_error': 'worker exited with code 1'}
            return original(**kwargs)

        driver.launcher = limited
        with self.assertRaisesRegex(RuntimeError, 'rate_limited'):
            driver.run()
        restarted = self.coordinator()
        restarted.launcher = limited
        with self.assertRaisesRegex(RuntimeError, 'rate_limited'):
            restarted.run()
        self.assertEqual(limited_models, ['primary-model', 'fallback-model'])

    def test_rate_limit_switch_after_running_worker_exits(self):
        config = yaml.safe_load(self.config_path.read_text())
        config['workers']['claude'].update(model='primary-model', fallback_model='fallback-model')
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([self.event()])
        driver = self.coordinator()
        original = driver.launcher
        models = []

        def limited(**kwargs):
            if kwargs['worker_agent_config']['name'] == 'claude':
                models.append(kwargs['model'])
                if kwargs['model'] == 'primary-model':
                    log = Path(kwargs['log_dir']) / 'stdout.log'
                    log.write_text(json.dumps({'type': 'turn.failed', 'error': {'message': '429 Too Many Requests'}}))
                    return {'ok': True, 'pid': 1073741824, 'process_exited': True,
                            'returncode': 1, 'stdout_path': str(log)}
            return original(**kwargs)

        driver.launcher = limited
        self.assertTrue(driver.run().is_file())
        self.assertEqual(models, ['primary-model', 'fallback-model'])

    def test_rate_limit_without_distinct_fallback_does_not_relaunch_stage(self):
        for fallback in (None, 'primary-model'):
            with self.subTest(fallback=fallback):
                fixture = ReviewCoordinatorTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                config = yaml.safe_load(fixture.config_path.read_text())
                config['workers']['claude'].update(model='primary-model', fallback_model=fallback)
                fixture.config_path.write_text(yaml.safe_dump(config))
                fixture.cycle([fixture.event()])
                driver = fixture.coordinator()
                original = driver.launcher
                calls = []

                def limited(**kwargs):
                    if kwargs['worker_agent_config']['name'] == 'claude':
                        calls.append(kwargs['model'])
                        log = Path(kwargs['log_dir']) / 'stdout.log'
                        log.write_text(json.dumps({'type': 'turn.failed', 'error': {'message': '429 Too Many Requests'}}))
                        return {'ok': False, 'pid': 1073741824, 'process_exited': True,
                                'returncode': 1, 'stdout_path': str(log), 'safe_error': 'worker exited with code 1'}
                    return original(**kwargs)

                driver.launcher = limited
                with self.assertRaisesRegex(RuntimeError, 'rate_limited'):
                    driver.run()
                self.assertEqual(calls, ['primary-model'])

    def test_sealing_rejects_fifo_without_waiting_for_a_writer(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        with review_store.connect(self.db) as conn:
            run = review_store.new_run(conn, driver.round_id, 'review', 'codex', driver.attempt_id,
                                       driver.manifest['artifact_root'], driver.manifest['prompt_sha256'])
        os.mkfifo(Path(run['output_dir'], 'result.json'))
        script = '''import json,sys
from robert_agent.review_coordinator import Coordinator
driver=object.__new__(Coordinator)
driver.manifest=json.loads(sys.argv[1])
try:
    driver.sealed_output(json.loads(sys.argv[2]))
except ValueError as error:
    assert 'regular file' in str(error), error
else:
    raise AssertionError('FIFO accepted')
'''
        result = subprocess.run([sys.executable, '-c', script, json.dumps(driver.manifest), json.dumps(run)],
                                timeout=2, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_other_reviewers_timeouts_do_not_advance_fallback(self):
        config = yaml.safe_load(self.config_path.read_text())
        for worker in config['workers'].values():
            worker.update(model='primary-model', fallback_model='fallback-model')
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([self.event()])
        original_wait = review_coordinator.Coordinator._wait

        def timeout(driver, run, launched, seconds):
            if run['stage'] == 'review':
                raise RuntimeError('review worker timed out')
            return original_wait(driver, run, launched, seconds)

        with patch.object(review_coordinator.Coordinator, '_wait', timeout):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                self.coordinator().run()
        self.coordinator().run()
        self.assertEqual({call[5] for call in self.calls if call[0] == 'review'}, {'primary-model'})

    def test_assignment_then_comment_runs_two_binaries_and_publishes_each_revision_once(self):
        binary = self.root / 'file_reviewer.py'
        binary.write_text('''import hashlib,json,os,re,sys
from pathlib import Path
prompt=sys.stdin.buffer.read()
text=prompt.decode()
manifest_path=Path(re.search(r'Read the registered context at (.*?) and source worktree ',text).group(1))
manifest=json.loads(manifest_path.read_text())
context=manifest_path.parent
source=Path(manifest['worktree_path'],'source.py').read_text()
assert Path.cwd()==Path(manifest['worktree_path'])
assert source.strip() in (context/'diff.patch').read_text()
output=Path(os.environ['ROBERT_REVIEW_OUTPUT_DIR'])
identity={key:manifest[key] for key in ('round_id','snapshot_id','head_sha')}
def save(name,value):
    path=output/(name+'.tmp')
    path.write_text(json.dumps(value))
    path.replace(output/name)
selected=[]
if 'Prepare shared context without findings' in text:
    stage='prepare'
    (output/'context-summary.md').write_text(source)
    save('source-index.json',{'files':[{'path':'source.py','reason':'changed source'}],'reading_notes':source})
elif 'Perform an independent code review.' in text:
    stage='review'
    assert (context/'context-summary.md').read_text()==source
    assert json.loads((context/'source-index.json').read_text())['reading_notes']==source
    save('result.json',dict(identity,schema_version=1,findings=[],dismissed_findings=[],
        coverage={'reviewed_paths':['source.py'],'unreviewed_paths':[],'notes':source},merge_recommendation='ready to merge'))
else:
    stage='synthesis'
    inputs=Path(re.search(r'Read the frozen selected artifact list (.*?)\\. Read each',text).group(1))
    records=json.loads(inputs.read_text())['records']
    assert len(records)==2
    for record in records:
        result=json.loads(Path(record['result_path']).read_text())
        assert result['coverage']['notes']==source and result['head_sha']==identity['head_sha']
        assert Path(record['report_path']).read_text()
        selected.append(record['result_path'])
    save('evaluation.json',dict(identity,schema_version=1,items=[],merge_recommendation='ready to merge',merge_reason='无问题'))
save('observed.json',dict(identity,stage=stage,source=source,prompt_sha256=hashlib.sha256(prompt).hexdigest(),selected=selected))
''')
        config = yaml.safe_load(self.config_path.read_text())
        for worker in config['workers'].values():
            worker['command'] = shlex.join([sys.executable, str(binary)])
        self.config_path.write_text(yaml.safe_dump(config))
        first_event = self.event() | {'event_type': 'assigned', 'actor_login': 'robert-bot',
            'assignment_actor_login': 'maintainer', 'assigned_to': 'robert-bot', 'body': '', 'intent': 'unclear'}
        remote_reviews, rounds = [], []

        def api(command, **kwargs):
            if '--paginate' in command:
                value = [remote_reviews]
            elif '--method' not in command:
                value = self.pr('', 42)
            else:
                value = {'id': 100 + len(remote_reviews), 'state': 'COMMENTED',
                         'commit_id': next(arg[10:] for arg in command if arg.startswith('commit_id=')),
                         'body': next(arg[5:] for arg in command if arg.startswith('body='))}
                value['html_url'] = f"https://github.com/example/backend/pull/42#pullrequestreview-{value['id']}"
                remote_reviews.append(value)
            return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr='')

        for event, value in ((first_event, 2), (self.event('second'), 3)):
            if value == 3:
                self.git_fixture.commit('value = 3\n')
            self.cycle([event, event])
            task, attempt = self.current()
            driver = review_coordinator.Coordinator(self.db, task, attempt, fetch_pr=self.pr)
            report = driver.run()
            rounds.append((driver, report.read_bytes()))
            self.cycle([])
            result = publish.publish_ready_actions(self.db, run_command=api)
            self.assertEqual(result['published_count'], 1, result)
            self.assertEqual(publish.publish_ready_actions(self.db, run_command=api)['published_count'], 0)
            self.cycle([], skip_publish=False)
            with review_store.connect(self.db) as conn:
                session = review_store.session(conn, task_id=task)
                self.assertEqual(session['status'], 'published')
                runs = review_store.runs(conn, driver.round_id)
            observed = [json.loads(Path(run['output_dir'], 'observed.json').read_text()) for run in runs]
            reviews = [item for item in observed if item['stage'] == 'review']
            self.assertEqual(len(reviews), 2)
            self.assertEqual(len({item['prompt_sha256'] for item in reviews}), 1)
            self.assertEqual({item['source'] for item in observed}, {f'value = {value}\n'})
            self.assertTrue(all(driver.round_id in path for item in observed for path in item['selected']))
        self.cycle([first_event, self.event('second')])
        with review_store.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_sessions').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 2)
        self.assertEqual(len(remote_reviews), 2)
        self.assertEqual([item['commit_id'] for item in remote_reviews],
                         [driver.manifest['head_sha'] for driver, _ in rounds])
        self.assertNotEqual(rounds[0][0].manifest['head_sha'], rounds[1][0].manifest['head_sha'])
        self.assertNotEqual(rounds[0][0].manifest['worktree_path'], rounds[1][0].manifest['worktree_path'])
        self.assertEqual(Path(rounds[0][0].manifest['artifact_root'], 'synthesis', 'merged-review.md').read_bytes(), rounds[0][1])

    def test_stdout_only_and_wrong_round_never_publish(self):
        self.cycle([self.event()])
        self.stdout_only = True
        with self.assertRaises((ValueError, RuntimeError, OSError)):
            self.coordinator().run()
        self.stdout_only, self.bad_round = False, True
        with self.assertRaises((ValueError, RuntimeError, OSError)):
            self.coordinator().run()
        with review_store.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM github_actions').fetchone()[0], 0)

    def test_file_landed_before_registration_is_adopted(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        original_register = driver.register
        def crash(run, artifact):
            if run['stage'] == 'review':
                raise KeyboardInterrupt('simulated crash after file rename')
            return original_register(run, artifact)
        with patch.object(driver, 'register', side_effect=crash), self.assertRaises(KeyboardInterrupt):
            driver.review()
        self.coordinator().run()
        self.assertEqual(Counter((c[0], c[1]) for c in self.calls)[('review', 'codex')], 1)

    def test_tampered_success_reruns_only_affected_model(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        records = driver.review()
        Path(records[0]['result_path']).write_text('{}')
        self.coordinator().run()
        counts = Counter((c[0], c[1]) for c in self.calls)
        self.assertEqual(counts[('review', 'codex')], 2)
        self.assertEqual(counts[('review', 'claude')], 1)

    def test_late_producer_write_cannot_change_registered_artifacts(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        records = driver.review()
        before = [Path(record['result_path']).read_bytes() for record in records]
        for call in self.calls:
            if call[0] == 'review':
                Path(call[4], 'result.json').write_text('late producer write')
        driver.synthesize(records)
        self.assertEqual([Path(record['result_path']).read_bytes() for record in records], before)

    def test_non_allowlisted_pr_author_keeps_legacy_worker(self):
        result = self.cycle([self.event(author='other')])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_sessions').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT expected_output FROM tasks').fetchone()[0], 'pr_review_comment')
        self.assertEqual(result['dispatch_count'], 1)

    def test_new_same_sha_trigger_is_new_round_and_untrusted_trigger_is_not(self):
        self.cycle([self.event(), self.event('untrusted', actor='stranger')])
        self.cycle([self.event(), self.event('second')])
        with review_store.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_sessions').fetchone()[0], 2)

    def test_enabling_native_review_does_not_replay_consumed_legacy_triggers(self):
        for event_type in ('comment', 'review_request'):
            with self.subTest(event_type=event_type):
                fixture = ReviewCoordinatorTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                config = yaml.safe_load(fixture.config_path.read_text())
                config['routes']['review-pr']['multi_agent_review']['enabled'] = False
                fixture.config_path.write_text(yaml.safe_dump(config))
                event = fixture.event('legacy-' + event_type)
                if event_type == 'review_request':
                    event.update(event_type='review_request', body='',
                                 requested_reviewer='robert-bot', requester_login='maintainer')
                fixture.cycle([event])
                task, _ = fixture.current()
                fixture.finish_task(task)
                with review_store.connect(fixture.db) as conn:
                    conn.execute("UPDATE task_events SET relationship='consumed' WHERE task_id=?", (task,))
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM task_events WHERE task_id=?', (task,)).fetchone()[0], 1)
                    before = conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
                config['routes']['review-pr']['multi_agent_review']['enabled'] = True
                fixture.config_path.write_text(yaml.safe_dump(config))

                fixture.cycle([event, event])

                with review_store.connect(fixture.db) as conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_sessions').fetchone()[0], 0)
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], before)

    def test_queued_legacy_replay_is_canceled_without_creating_a_task(self):
        config = yaml.safe_load(self.config_path.read_text())
        config['routes']['review-pr']['multi_agent_review']['enabled'] = False
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([self.event()])
        task, _ = self.current()
        self.finish_task(task)
        config['routes']['review-pr']['multi_agent_review']['enabled'] = True
        self.config_path.write_text(yaml.safe_dump(config))
        runtime = validate_config.validate_config(self.config_path, skip_external=True)
        repo = runtime['repos'][0]
        with review_store.connect(self.db) as conn:
            event = json.loads(conn.execute('SELECT payload_json FROM github_events LIMIT 1').fetchone()[0])
            route = run_once._effective_route(runtime, repo, event)
            queued = review_store.enqueue(conn, repo, event, route, route['native_review_workers'])

        self.cycle([])

        with review_store.connect(self.db) as conn:
            entry = review_store.session(conn, round_id=queued['round_id'])
            self.assertEqual(entry['status'], 'canceled')
            self.assertEqual(entry['error'], 'trigger_already_processed')
            self.assertIsNone(entry['task_id'])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
        self.cycle([self.event('fresh')])
        with review_store.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 2)

    def test_known_pr_review_request_uses_requester_trust_and_native_author_gate(self):
        self.cycle([self.event()])
        request = self.event('requested') | {'event_type': 'review_request', 'body': '',
            'requested_reviewer': 'robert-bot', 'requester_login': 'maintainer'}
        self.cycle([request, request, request | {'id': 'untrusted', 'actor_login': 'stranger',
                    'requester_login': 'stranger'}, request | {'id': 'wrong-target', 'requested_reviewer': 'someone-else'}])
        with review_store.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_sessions').fetchone()[0], 2)
        config = yaml.safe_load(self.config_path.read_text())
        config['routes']['review-pr']['multi_agent_review']['enabled'] = False
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([request | {'id': 'disabled'}])
        config['routes']['review-pr']['multi_agent_review']['enabled'] = True
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([request | {'id': 'unmatched', 'pr_author_login': 'other'}])
        with review_store.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_sessions').fetchone()[0], 2)

    def test_live_review_request_follows_old_assignment_and_uses_requester(self):
        config = yaml.safe_load(self.config_path.read_text())
        config['routes']['review-pr']['multi_agent_review']['author_allowlist'] = ['robert-bot']
        self.config_path.write_text(yaml.safe_dump(config))
        assigned = self.event('10', author='robert-bot') | {'event_type': 'assigned', 'actor_login': 'robert-bot',
            'assignment_actor_login': 'maintainer', 'assigned_to': 'robert-bot', 'body': '', 'intent': 'unclear'}
        self.cycle([assigned])
        timeline = [{'id': 10, 'event': 'assigned', 'actor': {'login': 'maintainer'},
                     'assignee': {'login': 'robert-bot'}, 'created_at': '2026-10-25T00:00:00Z'},
                    {'id': 11, 'event': 'review_requested', 'actor': {'login': 'maintainer'},
                     'requested_reviewer': {'login': 'robert-bot'}, 'created_at': '2026-10-25T01:00:00Z'}]

        def api(command, **kwargs):
            if command[:3] == ['gh', 'search', 'issues']:
                value = []
            elif command[:3] == ['gh', 'api', 'notifications']:
                value = [[{'id': 'notify-42', 'reason': 'review_requested', 'updated_at': '2026-10-25T01:00:00Z',
                           'repository': {'full_name': 'example/backend'},
                           'subject': {'type': 'PullRequest', 'url': 'https://api.github.com/repos/example/backend/pulls/42'}}]]
            elif command[2] in {'repos/example/backend/pulls/42', 'repos/example/backend/issues/42'}:
                value = self.pr('', 42) | {'user': {'login': 'robert-bot'}, 'title': 'PR', 'body': '',
                                         'html_url': 'https://github.com/example/backend/pull/42'}
            elif command[2] == 'repos/example/backend/issues/42/timeline':
                value = timeline
            else:
                raise AssertionError(command)
            return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr='')

        def poll():
            with patch.object(run_once, '_reconcile_remote_source_states', return_value=0), \
                 patch.object(run_once, '_collect_pr_remediation_decisions', return_value=[]):
                result = run_once.run_once(self.config_path, workflow_path=resource('workflow.yml'),
                    dry_run=True, skip_publish=True, discovery_runner=api)
            self.assertTrue(result['ok'], result)

        config['routes']['review-pr']['multi_agent_review']['enabled'] = False
        self.config_path.write_text(yaml.safe_dump(config))
        poll()
        with review_store.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_sessions').fetchone()[0], 1)
        config['routes']['review-pr']['multi_agent_review']['enabled'] = True
        self.config_path.write_text(yaml.safe_dump(config))
        poll()
        poll()
        with review_store.connect(self.db) as conn:
            sessions = conn.execute('SELECT trigger_fingerprint FROM review_sessions').fetchall()
            self.assertEqual({row[0] for row in sessions}, {'assigned:10', 'review_request:11'})
        timeline.append({'id': 12, 'event': 'review_requested', 'actor': {'login': 'stranger'},
                         'requested_reviewer': {'login': 'robert-bot'}, 'created_at': '2026-10-25T02:00:00Z'})
        poll()
        with review_store.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_sessions').fetchone()[0], 2)

    def test_record_audit_publish_finalize_and_drain_next_round(self):
        self.cycle([self.event()])
        first = self.coordinator()
        first.run()
        self.cycle([self.event('second')])
        with review_store.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT audit_status FROM github_actions').fetchone()[0], 'accepted')
        calls = []
        def api(command, **kwargs):
            calls.append(command)
            if '--paginate' in command:
                value = [[]]
            elif '--method' not in command:
                value = self.pr('', 42)
            else:
                value = {'id': 100, 'commit_id': first.manifest['head_sha'], 'state': 'COMMENTED',
                         'body': next(arg[5:] for arg in command if arg.startswith('body=')),
                         'html_url': 'https://github.com/example/backend/pull/42#pullrequestreview-100'}
            return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr='')
        result = publish.publish_ready_actions(self.db, run_command=api)
        self.assertEqual(result['published_count'], 1, result)
        self.assertEqual(sum('--method' in call for call in calls), 1)
        self.cycle([], skip_publish=False)
        with review_store.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT lifecycle FROM tasks WHERE task_id=?', (first.task_id,)).fetchone()[0], 'completed')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 2)
            self.assertEqual(review_store.session(conn, task_id=first.task_id)['status'], 'published')

    def test_superseded_report_releases_queue_without_publication(self):
        self.cycle([self.event()])
        first = self.coordinator()
        first.run()
        self.cycle([self.event('second')])
        self.git_fixture.commit('value = 4\n')
        def api(command, **kwargs):
            self.assertNotIn('--method', command)
            return SimpleNamespace(returncode=0, stdout=json.dumps([[]] if '--paginate' in command else self.pr('', 42)), stderr='')
        outcome = publish.publish_ready_actions(self.db, run_command=api)
        self.assertEqual(outcome['skipped_count'], 1, outcome)
        self.cycle([], skip_publish=False)
        with review_store.connect(self.db) as conn:
            self.assertEqual(review_store.session(conn, task_id=first.task_id)['status'], 'superseded')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 2)

    def test_synthesis_retry_does_not_rerun_reviewers(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        original = driver.launcher
        def fail_synthesis(**kwargs):
            if Path(kwargs['prompt_path']).name.startswith('synthesis-prompt-'):
                return {'ok': False, 'safe_error': 'synthesis unavailable'}
            return original(**kwargs)
        driver.launcher = fail_synthesis
        with self.assertRaisesRegex(RuntimeError, 'synthesis unavailable'):
            driver.run()
        self.coordinator().run()
        counts = Counter((c[0], c[1]) for c in self.calls)
        self.assertEqual(counts[('review', 'codex')], 1)
        self.assertEqual(counts[('review', 'claude')], 1)

    def test_cancel_fences_late_file_and_publication(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        original = driver.launcher
        def cancel(**kwargs):
            result = original(**kwargs)
            with review_store.connect(self.db) as conn:
                conn.execute("UPDATE tasks SET lifecycle='canceled' WHERE task_id=?", (driver.task_id,))
            return result
        driver.launcher = cancel
        with self.assertRaises(review_coordinator.ReviewStopped):
            driver.run()
        with review_store.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM github_actions').fetchone()[0], 0)
            self.assertEqual(review_store.session(conn, task_id=driver.task_id)['status'], 'canceled')

    def test_unbound_queue_after_crash_wakes_local_drain_in_repo_scope(self):
        self.cycle([self.event()])
        task, _ = self.current()
        self.cycle([self.event('second')])
        self.finish_task(task)
        with review_store.connect(self.db) as conn:
            repo_id = conn.execute('SELECT repo_id FROM repos').fetchone()[0]
            self.assertTrue(loop_engine.has_runnable_local_work(conn, repo_id=repo_id))
            self.assertFalse(loop_engine.has_runnable_local_work(conn, repo_id='different-repo'))
            conn.execute("UPDATE review_sessions SET status='canceled' WHERE task_id IS NULL")
            self.assertFalse(loop_engine.has_runnable_local_work(conn, repo_id=repo_id))

    def test_disabled_or_unmatched_followup_preserves_legacy_route(self):
        config = validate_config.validate_config(self.config_path, skip_external=True)
        event = {**self.event(), 'authorization_status': 'accepted_context'}
        self.assertEqual(run_once._effective_route(config, config['repos'][0], event)['route_id'], 'review-pr')
        for author, enabled in [('other', True), ('author', False)]:
            config['routes']['review-pr']['multi_agent_review']['enabled'] = enabled
            result = run_once._effective_route(config, config['repos'][0], {**event, 'pr_author_login': author})
            self.assertEqual(result['route_id'], 'review-comment')
            self.assertNotIn('native_review_workers', result)

    def test_changed_result_after_failed_synthesis_gets_new_sealed_inputs(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        records = driver.review()
        driver.launcher = lambda **kwargs: {'ok': False, 'safe_error': 'synthesis unavailable'}
        with self.assertRaisesRegex(RuntimeError, 'synthesis unavailable'):
            driver.synthesize(records)
        original = Path(driver.manifest['artifact_root'], 'synthesis', 'inputs.json').read_bytes()
        Path(records[0]['result_path']).write_text('{}')
        self.coordinator().run()
        self.assertEqual(Path(driver.manifest['artifact_root'], 'synthesis', 'inputs.json').read_bytes(), original)
        counts = Counter((c[0], c[1]) for c in self.calls)
        self.assertEqual(counts[('review', 'codex')], 2)
        self.assertEqual(counts[('review', 'claude')], 1)

    def test_owner_attention_blocks_next_round_even_if_session_status_is_old(self):
        self.cycle([self.event()])
        task, _ = self.current()
        self.cycle([self.event('second')])
        self.finish_task(task)
        with review_store.connect(self.db) as conn:
            conn.execute("UPDATE tasks SET lifecycle='failed' WHERE task_id=?", (task,))
            conn.execute("UPDATE execution_subjects SET binding_status='attention' WHERE subject_id=?", (task,))
            conn.execute("UPDATE review_sessions SET status='reviewing' WHERE task_id=?", (task,))
            self.assertFalse(loop_engine.has_runnable_local_work(conn))
        self.cycle([])
        with review_store.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)

    def test_unregistered_old_synthesis_is_not_adopted_after_input_repair(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        records = driver.review()
        with patch.object(driver, 'register', side_effect=KeyboardInterrupt('crash before synthesis registration')):
            with self.assertRaises(KeyboardInterrupt):
                driver.synthesize(records)
        Path(records[0]['result_path']).write_text('{}')
        self.coordinator().run()
        counts = Counter((c[0], c[1]) for c in self.calls)
        self.assertEqual(counts[('synthesis', 'codex')], 2)
        with review_store.connect(self.db) as conn:
            syntheses = [r for r in review_store.runs(conn, driver.round_id) if r['stage'] == 'synthesis']
        self.assertEqual(len(syntheses), 2)
        self.assertNotEqual(syntheses[0]['prompt_sha256'], syntheses[1]['prompt_sha256'])

    @unittest.skipUnless(hasattr(os, 'killpg'), 'requires process groups')
    def test_exited_model_with_live_child_cannot_register_result(self):
        self.check_lingering_child('success')

    @unittest.skipUnless(hasattr(os, 'killpg'), 'requires process groups')
    def test_failed_startup_cleans_descendants_before_releasing_capacity(self):
        self.check_lingering_child('failure')

    @unittest.skipUnless(hasattr(os, 'killpg'), 'requires process groups')
    def test_timeout_with_child_ignoring_termination_requires_attention(self):
        self.check_lingering_child('timeout')

    def check_lingering_child(self, mode):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        binary = self.root / 'model_with_child.py'
        binary.write_text('''import os,subprocess,sys,time
from pathlib import Path
sys.stdin.read()
subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])
Path(os.environ['ROBERT_REVIEW_OUTPUT_DIR'],'result.json').write_text('{}')
''' + ("time.sleep(30)\n" if mode == 'timeout' else f"sys.exit({1 if mode == 'failure' else 0})\n"))
        driver.launcher = dispatch.dispatch_worker
        driver.request['workers'][0].update(agent='command', command_argv=[sys.executable, str(binary)])
        driver.request['workers'] = driver.request['workers'][:1]
        if mode == 'timeout':
            driver.request['workers'][0]['timeout_seconds'] = 1
        try:
            expected_error = RuntimeError if mode == 'failure' else review_coordinator.ReviewStopped
            expected_message = 'worker exited immediately' if mode == 'failure' else 'process group'
            with self.assertRaisesRegex(expected_error, expected_message):
                driver.run()
            with review_store.connect(self.db) as conn:
                runs = review_store.runs(conn, driver.round_id)
                self.assertFalse(any(run['stage'] == 'review' and run['status'] == 'succeeded' for run in runs))
                self.assertEqual(review_store.session(conn, task_id=driver.task_id)['status'],
                                 'failed' if mode == 'failure' else 'attention')
                if mode == 'failure':
                    for run in runs:
                        if run['stage'] == 'review':
                            self.assertFalse(run_once._live_process_group(run['dispatch']['pid']))
                            self.assertFalse(run['slot_reserved'])
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM github_actions').fetchone()[0], 0)
        finally:
            # These groups were created exclusively by this fixture's launcher.
            with review_store.connect(self.db) as conn:
                for run in review_store.runs(conn, driver.round_id):
                    pid = run['dispatch'].get('pid')
                    if run['stage'] == 'review' and pid:
                        try:
                            os.killpg(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
