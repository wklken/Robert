from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.adapters.sqlite import database as storage
from robert_agent.supervision import git as supervision_git
from robert_agent.workflows.maintenance import repair as supervision_repair
from robert_agent.work.application import commands as work_items
from robert_agent.adapters.processes import identity as worker_identity


SUBMISSION = {
    'consumed_work_item_event_ids': [], 'used_skills': [],
    'verification': [{'command': ['python', '-m', 'unittest'], 'status': 'passed', 'purpose': 'regression',
                      'required': True, 'exit_code': 0, 'source': 'worker_reported'}],
    'planned_github_actions': [{'type': 'push_existing_pr'},
                               {'type': 'open_pr', 'title': 'Fix fixture', 'body': 'Repair fixture behavior.'}],
}


def submission_source():
    return (f'submission = json.loads({json.dumps(SUBMISSION)!r})\n'
            'submission["consumed_work_item_event_ids"] = json.loads(request["prompt"])["work_item_event_ids"]\n'
            'print(json.dumps(submission))\n')

_BROKER = '''
import json,sys
from pathlib import Path
from robert_agent.workflows.maintenance import repair as supervision_repair
from robert_agent.workflows.maintenance import worker as supervision_repair_worker
from robert_agent.configuration import command as validate_config
config_file, task, attempt, real_probe = sys.argv[1:]
def read_config(path, **kwargs):
    return {'ok': True, **json.loads(Path(path).read_text())}
validate_config.validate_config = read_config
if real_probe != 'true':
    supervision_repair.probe_restricted_launcher = lambda *a, **k: {'ok': True, 'status': 'fixture_only'}
result = supervision_repair_worker.run(read_config(config_file), task, attempt, sys.stdin.read())
print(json.dumps(result), flush=True)
'''


def wait_for_file(path):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size:
            return path.read_text()
        time.sleep(.01)
    raise AssertionError(f'Fixture did not reach {path.name}')


class BrokerFixture:
    """A real temporary checkout, persisted attempt, and process-boundary fixture."""

    def __init__(self, root, model_source, *, timeout=5, launcher=None):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.trees = self.root / 'trees'
        self.trees.mkdir()
        self.data = self.root / 'data'
        self.db = self.data / 'state.sqlite3'
        self.config_file = self.root / 'trusted-config.json'
        self.model = self.root / 'model.py'
        self.model.write_text(model_source, encoding='utf-8')
        self.model.chmod(0o600)
        self.git('init', '-q', '-b', 'main')
        (self.repo / 'fixture.txt').write_text('before\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'baseline')
        prepared = supervision_git.prepare({
            'repo_root': str(self.repo), 'worktree_root': str(self.trees),
            'default_base_branch': 'main', 'full_name': 'fixture/robert',
        }, 'task-fixture', self.data)
        self.tree = Path(prepared['worktree_path'])
        self.git_binding = prepared['repair_git']
        (self.tree / 'fixture.txt').write_text('after\n')
        self.git('commit', '-qam', 'fixture repair', cwd=self.tree)
        if launcher is None:
            passthrough = self.root / 'launcher.py'
            passthrough.write_text('import os,sys\nos.execv(sys.argv[2],sys.argv[2:])\n')
            passthrough.chmod(0o600)
            launcher = [sys.executable, str(passthrough)]
        self.config = {'db_path': str(self.db), 'data_dir': str(self.data), 'config_path': str(self.config_file),
            'repos': [{'full_name': 'fixture/robert', 'repo_root': str(self.repo), 'worktree_root': str(self.trees),
                       'default_base_branch': 'main', 'enabled': True}],
            'workers': [{'name': 'repair', 'agent': 'command', 'command_argv': [sys.executable, str(self.model)],
                         'environment_allowlist': [], 'timeout_seconds': timeout}],
            'supervision': {'mode': 'observe', 'repair': {'mode': 'auto', 'repository': 'fixture/robert',
                'worker': 'repair', 'allowed_actions': list(supervision_repair.ACTIONS), 'launcher': launcher,
                'verification_commands': [[sys.executable, '-c', 'pass']]}}}
        self.save_config()
        storage.init_database(self.db)
        self.now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute('''INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root)
                VALUES ('repo','fixture/robert','bot','main',?,?)''', (str(self.repo), str(self.trees)))

    def git(self, *args, cwd=None):
        return subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                               '-c', 'commit.gpgsign=false', *args], cwd=cwd or self.repo, check=True,
                              capture_output=True, text=True, timeout=10).stdout.strip()

    def save_config(self):
        temporary = self.config_file.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.config))
        temporary.chmod(0o600)
        temporary.replace(self.config_file)

    def bind(self, task_id=None):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            if task_id is None:
                policy = supervision_repair.normalize_repair(self.config['supervision']['repair'])
                auth = supervision_repair._auto_authorization(self.config, policy, self.config['repos'][0])
                repair, _ = work_items.create_supervision_repair(conn, repo_id='repo', title='Fixture repair',
                    description='Reproduce fixture', repair={'incident_id': 'fixture-incident', 'root_key': 'fixture-root',
                        'automatic': True, 'worker': 'repair', 'policy_digest': auth['policy_digest']},
                    now=self.now, idempotency_key='supervision-repair:fixture')
                task_id = repair['task_id']
            self.task = task_id
            self.attempt = 'fixture-attempt'
            conn.execute("UPDATE tasks SET lifecycle='running',route_id='new-pr',expected_output='new_pr' WHERE task_id=?", (task_id,))
            conn.execute('''INSERT INTO attempts(attempt_id,task_id,attempt_no,status,worktree_path,branch_name,metadata_json)
                VALUES (?,?,1,'running',?,?,?)''', (self.attempt, task_id, str(self.tree),
                self.git_binding['branch'], json.dumps({'result_contract': 'compact-v1', 'repair_git': self.git_binding})))
            conn.execute('''INSERT INTO route_decisions(route_decision_id,task_id,route_id,expected_output,
                allowed_github_actions_json,confidence,created_at) VALUES ('route',?,'new-pr','new_pr',?,'high',?)''',
                         (task_id, json.dumps(list(supervision_repair.ACTIONS)), self.now))

    def launch(self, *, real_probe=False, private_group=True):
        command = [sys.executable, '-B', '-c', _BROKER, str(self.config_file), self.task, self.attempt,
                   'true' if real_probe else 'false']
        process = subprocess.Popen(command, cwd=PACKAGE_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=private_group, text=True)
        with closing(sqlite3.connect(self.db)) as conn:
            events = conn.execute('''SELECT e.event_id FROM work_item_events e
                JOIN work_items wi ON wi.work_item_id=e.work_item_id JOIN tasks t ON t.workstream_id=wi.workstream_id
                WHERE t.task_id=? ORDER BY e.created_at,e.event_id''', (self.task,)).fetchall()
        process.stdin.write(json.dumps({'request': 'Apply this fixture repair.', 'work_item_event_ids': [row[0] for row in events]}))
        process.stdin.close()
        process.stdin = None
        return process

    def counts(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return tuple(conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                         for table in ('worker_results', 'github_actions'))


def broker_result(process):
    stdout, stderr = process.communicate(timeout=9)
    if process.returncode != 0:
        raise AssertionError(f'Broker exited {process.returncode}. {stderr}')
    return json.loads(stdout)


def stop_fixture(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=3)
    if process.pid != os.getpgrp() and worker_identity.live_process_group(process.pid):
        os.killpg(process.pid, signal.SIGKILL)


class BrokerProcessTests(unittest.TestCase):
    def fixture(self, source, *, timeout=5):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        fixture = BrokerFixture(Path(temp.name), source, timeout=timeout)
        fixture.bind()
        return fixture

    def launch(self, fixture, **options):
        process = fixture.launch(**options)
        self.addCleanup(stop_fixture, process)
        return process

    def test_trusted_broker_ignores_repository_python_package(self):
        from robert_agent.adapters.processes import worker as dispatch
        from robert_agent.workflows.maintenance import worker as supervision_repair_worker
        fixture = self.fixture('raise AssertionError("model must not start")')
        package = fixture.tree / 'robert_agent'
        package.mkdir()
        marker = fixture.root / 'host-import-marker'
        (package / '__init__.py').write_text(f'from pathlib import Path; Path({str(marker)!r}).touch()')
        prompt = fixture.root / 'prompt.txt'
        prompt.write_text('fixture')
        profile = supervision_repair_worker.launch_profile(fixture.config, fixture.task, fixture.attempt,
                                                            fixture.config['workers'][0])
        launched = dispatch.dispatch_worker(fixture.task, fixture.attempt, prompt, worktree_path=fixture.tree,
                                           worker_agent_config=profile, dry_run=False, startup_probe_seconds=.5)
        self.assertFalse(marker.exists(), launched)
        self.assertEqual(launched.get('returncode'), 1, launched)
        stderr = Path(launched['stderr_path']).read_text()
        self.assertNotIn('No module named robert_agent.workflows.maintenance.worker', stderr)

    def test_valid_output_uses_existing_result_submission(self):
        fixture = self.fixture('import json,sys\nrequest = json.load(sys.stdin)\n'
                               'assert request["result_transport"] == "stdout_compact_submission"\n' + submission_source())
        report = broker_result(self.launch(fixture))
        self.assertTrue(report['ok'], report)
        self.assertEqual(fixture.counts(), (1, 2))
        with closing(sqlite3.connect(fixture.db)) as conn:
            row = conn.execute('SELECT task_id,attempt_id,output_type FROM worker_results').fetchone()
            self.assertEqual(row, (fixture.task, fixture.attempt, 'new_pr'))

    def test_invalid_and_oversized_output_cannot_reserve_a_result(self):
        for source, expected in (
            ('print("[]")', 'invalid_repair_submission'),
            ('print("x" * (130 * 1024))', 'repair_result_too_large'),
            (f'print({json.dumps({**SUBMISSION, "task_id": "forged"})!r})', 'failed_validation'),
        ):
            with self.subTest(expected=expected):
                fixture = self.fixture(source)
                self.assertEqual(broker_result(self.launch(fixture))['status'], expected)
                self.assertEqual(fixture.counts(), (0, 0))

    def sleepy(self, timeout):
        source = '''
import pathlib, subprocess, sys, time
child = "import os,pathlib,signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
subprocess.Popen([sys.executable, '-c', child, str(pathlib.Path(__file__).parent / 'inner-child')])
time.sleep(30)
'''
        return self.fixture(source, timeout=timeout)

    def test_timeout_and_cancellation_reap_term_resistant_grandchild(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                fixture = self.sleepy(timeout=8 if cancel else 1)
                process = self.launch(fixture)
                child_pid = int(wait_for_file(fixture.root / 'inner-child'))
                self.assertEqual(os.getpgid(child_pid), process.pid)
                if cancel:
                    process.terminate()
                report = broker_result(process)
                self.assertEqual(report['status'], 'repair_worker_stopped' if cancel else 'repair_worker_timeout')
                self.assertFalse(worker_identity.live_process_group(process.pid))
                self.assertEqual(fixture.counts(), (0, 0))

    def test_revoked_policy_after_launch_cannot_submit(self):
        fixture = self.fixture('''
import pathlib,time
root = pathlib.Path(__file__).parent
(root / 'ready').write_text('yes')
while not (root / 'finish').exists():
    time.sleep(.01)
''' + f'print({json.dumps(SUBMISSION)!r})\n')
        process = self.launch(fixture)
        wait_for_file(fixture.root / 'ready')
        fixture.config['supervision']['repair']['allowed_actions'] = ['open_pr']
        fixture.save_config()
        (fixture.root / 'finish').touch()
        report = broker_result(process)
        self.assertEqual(report['status'], 'repair_policy_rejected')
        self.assertEqual(fixture.counts(), (0, 0))

    def test_broker_requires_its_own_process_group(self):
        fixture = self.fixture('raise AssertionError("must not launch")')
        report = broker_result(self.launch(fixture, private_group=False))
        self.assertEqual(report['status'], 'repair_broker_group_not_isolated')
        self.assertEqual(fixture.counts(), (0, 0))
