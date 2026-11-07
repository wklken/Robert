"""Shared fixtures for review coordinator tests."""
from robert_agent.adapters.processes import identity as worker_identity
import json
import os
import re
from pathlib import Path
import signal
import sys
import unittest
import yaml
from tests.support import review_workspace as workspace_fixture
from robert_agent.adapters.processes import worker as dispatch
from robert_agent.workflows.review import coordinator as review_coordinator
from robert_agent.workflows.review import files as review_files
from robert_agent.adapters.sqlite import review_rounds as review_store
from robert_agent.runtime import cycle as run_once
from robert_agent.operations.initialize import init_config
from robert_agent.resource_files import resource


class ReviewCoordinatorFixture(unittest.TestCase):
    def setUp(self):
        self.git_fixture = workspace_fixture.ReviewWorkspaceFixture()
        self.git_fixture.setUp()
        self.addCleanup(self.git_fixture.doCleanups)
        self.root = self.git_fixture.root
        self.config_path = self.root / 'config.yml'
        init_config(self.config_path, {'repo': 'example/backend', 'repo_path': str(self.git_fixture.repo),
                    'worker': 'codex', 'github_account': 'robert-bot', 'trusted_actor': 'maintainer'}, non_interactive=True)
        config = yaml.safe_load(self.config_path.read_text())
        config['data_dir'] = str(self.root / 'data')
        worker = next(iter(config['workers'].values()))
        config['default_worker'] = 'codex'
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
            expected_error = RuntimeError if mode == 'failure' else review_coordinator.StageStopped
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
                            self.assertFalse(worker_identity.live_process_group(run['dispatch']['pid']))
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
