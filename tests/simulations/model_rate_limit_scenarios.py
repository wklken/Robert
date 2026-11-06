"""Real worker processes against a scripted loopback model API.

The fixture HTTP client implements its own bounded per-attempt retries. Robert
still owns dispatch, process supervision, fallback, result ingestion and audit.
"""

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys

from robert_agent import daemon_state, dispatch, run_once, supervisor, work_items
from tests.simulations.supervision_scenarios import query, wait_for
from tests.support_model_http import model_http


PRIMARY = 'model-primary'
FALLBACK = 'model-fallback'
REPO_ID = 'repo:example/backend'
REPO_NAME = 'example/backend'
RETRY_AFTER = '2'

_WORKER = '''
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time
from urllib.error import HTTPError
from urllib.request import build_opener, ProxyHandler

from robert_agent.worker.result import build_result, record_result
from robert_agent.worker.snapshot import record_snapshot

db, endpoint, tries = sys.argv[1:4]
task = Path(sys.argv[sys.argv.index('--add-dir') + 1]).name
model = sys.argv[sys.argv.index('--model') + 1]
sys.stdin.read()
with closing(sqlite3.connect(db)) as conn:
    attempt, = conn.execute(
        'SELECT attempt_id FROM attempts WHERE task_id=? ORDER BY attempt_no DESC LIMIT 1', (task,)
    ).fetchone()
    number, fingerprint = conn.execute(
        """SELECT source.number,event.event_fingerprint
           FROM task_events linked JOIN github_events event ON event.event_id=linked.event_id
           JOIN github_sources source ON source.source_id=event.source_id
           WHERE linked.task_id=? AND linked.relationship='trigger'""", (task,)
    ).fetchone()

opener = build_opener(ProxyHandler({}))
for index in range(int(tries)):
    try:
        with opener.open(f'{endpoint}/worker/{number}/{model}', timeout=4) as response:
            json.load(response)
        break
    except HTTPError as error:
        with error:
            if error.code != 429:
                raise
            retry_after = error.headers['Retry-After']
        record_snapshot(db, task, attempt, 'execute', 'running', 'Waiting for model quota')
        if index + 1 == int(tries):
            print(json.dumps({'type': 'turn.failed', 'error': {
                'message': f'HTTP 429 Too Many Requests\\nRetry-After: {retry_after}'
            }}), flush=True)
            sys.exit(1)
        print(json.dumps({'type': 'error', 'message': 'HTTP 429 retrying'}), flush=True)
        time.sleep(int(retry_after))

result = record_result(db, build_result(task, attempt, 'local_result', planned_github_actions=[],
    consumed_event_fingerprints=[fingerprint], verification=[], handoff='Model response received',
    used_skills=[]))
assert result['ok'], result
print(json.dumps({'type': 'turn.completed'}), flush=True)
'''


def _now():
    return datetime.now(timezone.utc)


def worker_path(model=PRIMARY, number=123):
    return f'/worker/{number}/{model}'


class WorkerRuntime:
    def __init__(self, root, endpoint, tasks, tries):
        self.data = root / 'data'
        self.db = self.data / 'state.sqlite3'
        self.processes = []
        repo_root = root / 'repo'
        (repo_root / '.git').mkdir(parents=True)
        self.repo = {'full_name': REPO_NAME, 'github_account': 'robert-bot', 'trusted_actors': ['operator'],
                     'default_base_branch': 'main', 'repo_root': str(repo_root),
                     'worktree_root': str(repo_root / '.worktrees')}
        (repo_root / '.worktrees').mkdir()
        config_path = root / 'config.json'
        document = {
            'version': 2, 'data_dir': str(self.data), 'database': self.db.name,
            'github': {'account': 'robert-bot'}, 'default_worker': 'default',
            'workers': {'default': {'adapter': 'cbc', 'command': 'cbc',
                                    'model': 'default', 'effort': 'default'}},
            'scheduler': {'max_concurrency': tasks},
            'repos': [{'full_name': REPO_NAME, 'trusted_actors': ['operator'],
                       'default_branch': 'main', 'checkout': str(repo_root),
                       'worktrees': str(repo_root / '.worktrees')}],
        }
        config_path.write_text(json.dumps(document))
        self.config = run_once.validate_config.validate_config(config_path, skip_external=True)
        assert self.config['ok'], self.config
        discovery = root / 'discovery.json'
        discovery.write_text(json.dumps({'events': [
            {'id': f'comment-{number}', 'number': number, 'source_type': 'issue',
             'event_type': 'comment', 'actor_login': 'operator', 'body': '@robert-bot fix this bug',
             'intent': 'bug_fix', 'url': f'https://github.com/{REPO_NAME}/issues/{number}',
             'state': 'open', 'source_updated_at': _now().isoformat()}
            for number in range(123, 123 + tasks)
        ]}))
        planned = run_once.run_once(config_path, fixture_path=discovery, dry_run=True, skip_external=True)
        assert planned['ok'], planned
        with closing(sqlite3.connect(self.db)) as conn, conn:
            attempts = conn.execute('SELECT worktree_path FROM attempts').fetchall()
            assert len(attempts) == tasks, planned
            for path, in attempts:
                Path(path).mkdir(parents=True)
            # The intake fixture builds ordinary GitHub attempts. Use a local
            # result contract to verify ingestion without external publication.
            conn.execute("UPDATE tasks SET expected_output='local_result',route_id='local-result'")
            conn.execute("UPDATE route_decisions SET expected_output='local_result',route_id='local-result',"
                         "allowed_github_actions_json='[]',required_skills_json='[]'")
            now = _now().isoformat()
            daemon_state.start_daemon_run(conn, config_path=str(config_path), owner_id='model-fixture', now=now)
            lease = daemon_state.acquire_daemon_lease(conn, resource_key='database:' + str(self.db.resolve()),
                                                     owner_id='model-fixture', ttl_seconds=120, now=now)
            assert lease['ok'], lease
        self.config.update(db_path=str(self.db), supervision={'mode': 'observe', 'poll_seconds': 1, 'stall_seconds': 1})
        worker_script = root / 'worker.py'
        worker_script.write_text(_WORKER)
        self.worker = {'name': 'default', 'command': [sys.executable, str(worker_script), str(self.db), endpoint.url, str(tries)],
                       'agent': 'tcodex', 'default_model': PRIMARY, 'fallback_model': FALLBACK, 'default_effort': 'high',
                       'timeout_seconds': 30}

    def attempts(self):
        return query(self.db, '''SELECT task_id,attempt_id,status,json_extract(metadata_json,'$.dispatch.model'),
                                        json_extract(failure_json,'$.error_class')
                                 FROM attempts ORDER BY task_id,attempt_no''')

    def dispatch(self):
        with closing(sqlite3.connect(self.db)) as conn:
            queue = []
            for attempt_id, in conn.execute("SELECT attempt_id FROM attempts WHERE status='prepared'"):
                current = run_once._current_dispatch_target(conn, attempt_id)
                if current:
                    task, tree = current
                    queue.append({'task_info': task, 'worktree_result': tree, 'repo_id': REPO_ID, 'repo': self.repo})
        launches = run_once._dispatch_queued(self.db, [REPO_ID], queue, False, self.config['max_concurrency'],
                                             workers=[self.worker])
        for item in launches:
            proc = dispatch._BACKGROUND_PROCESSES.get(item.get('pid'))
            if proc:
                self.processes.append(proc)
        assert all(item['ok'] for item in launches), launches
        return launches

    def supervise(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            run_once._supervise_running_attempts(conn, REPO_ID, self.config, _now().isoformat(),
                                                data_dir=self.data, db_path=self.db, repo=self.repo)

    def observe(self, now=None):
        observed = supervisor.check(self.config, record=True, now=(now or _now()).isoformat())
        assert observed['ok'], observed
        return observed['observations']

    def finish(self):
        for proc in self.processes:
            proc.wait(timeout=12)
        self.supervise()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            run_once._audit_completed_results(conn, REPO_ID, self.data, self.db, self.repo, 'model-fixture',
                                              _now().isoformat(), False, [], 0, self.config)


@contextmanager
def worker_runtime(root, endpoint, *, tasks=1, tries=2):
    """Prepare real Robert state and own every worker started by the scenario."""
    runtime = WorkerRuntime(Path(root).resolve(), endpoint, tasks, tries)
    try:
        yield runtime
    finally:
        for proc in runtime.processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


def _requests(endpoint, path, expected):
    requests = endpoint.requests(path)
    assert [request['status'] for request in requests] == expected, requests
    intervals = [later['at'] - earlier['at'] for earlier, later in zip(requests, requests[1:])]
    assert all(interval >= int(request['retry_after']) for interval, request in zip(intervals, requests)), requests
    return {'statuses': expected, 'retry_intervals_seconds': [round(value, 3) for value in intervals]}


def _progress(observations):
    return next(item for item in observations if item['rule_id'] == 'worker_no_progress')


def _assert_delivery(runtime, count):
    assert query(runtime.db, 'SELECT lifecycle FROM tasks ORDER BY task_id') == [('completed',)] * count
    assert query(runtime.db, "SELECT json_extract(metadata_json,'$.audit.status') FROM worker_results") == [
        ('accepted',)
    ] * count
    assert query(runtime.db, 'SELECT COUNT(*) FROM github_actions') == [(0,)]
    assert len(runtime.attempts()) == count


def _recovers(root, misses):
    path = worker_path()
    with model_http({path: [(429, RETRY_AFTER)] * misses + [(200, None)]}) as endpoint:
        with worker_runtime(root, endpoint, tries=misses + 1) as runtime:
            launched, = runtime.dispatch()
            wait_for(lambda: len(endpoint.requests(path)) == 1, 'first HTTP 429')
            sampled = _now()
            first = _progress(runtime.observe(sampled))
            assert first['classification'] == 'expected_wait', first
            if misses > 1:
                initial_heartbeat = query(runtime.db, 'SELECT heartbeat_at FROM attempts')
                wait_for(lambda: len(endpoint.requests(path)) == 2, 'second HTTP 429')
                wait_for(lambda: query(runtime.db, 'SELECT heartbeat_at FROM attempts') != initial_heartbeat,
                         'heartbeat during rate limit')
                suspect = _progress(runtime.observe(max(_now(), sampled + timedelta(seconds=1))))
                assert suspect['classification'] == 'suspect' and not suspect.get('progress_token'), suspect
                assert suspect['eligible_since'] == first['eligible_since'], suspect
                runtime.supervise()
                assert runtime.attempts()[0][2:4] == ('running', PRIMARY), runtime.attempts()
                assert runtime.dispatch() == []
            runtime.finish()
            _assert_delivery(runtime, 1)
            complete = _progress(runtime.observe())
            assert complete['classification'] == 'healthy' and complete['reason_code'] == 'worker_result_received', complete
            assert complete['positive_evidence']['attempt_id'] == launched['attempt_id'], complete
            assert query(runtime.db, "SELECT COUNT(*) FROM supervision_incidents WHERE scope_type='task'") == [(0,)]
            return {'scenario': 'model_429_then_200' if misses == 1 else 'model_repeated_429_then_200',
                    'passed': True, 'attempts': 1, 'results': 1,
                    'requests': _requests(endpoint, path, [429] * misses + [200])}


def _exhausted(root):
    primary, fallback = worker_path(), worker_path(FALLBACK)
    with model_http({primary: [(429, RETRY_AFTER)], fallback: [(429, RETRY_AFTER)]}) as endpoint:
        with worker_runtime(root, endpoint) as runtime:
            runtime.dispatch()
            runtime.finish()
            assert [row[2:4] for row in runtime.attempts()] == [('failed', PRIMARY), ('prepared', None)]
            runtime.dispatch()
            runtime.finish()
            assert [row[2:] for row in runtime.attempts()] == [('failed', PRIMARY, 'rate_limited'),
                                                              ('failed', FALLBACK, 'rate_limited')]
            for _ in range(2):
                runtime.supervise()
                runtime.observe()
                assert runtime.dispatch() == []
            status = supervisor.status(runtime.config)
            failures = [item for item in status['incidents'] if item['rule_id'] == 'execution_failed']
            assert len(failures) == 1, status
            assert query(runtime.db, 'SELECT recovery_attempts FROM execution_subjects') == [(1,)]
            assert query(runtime.db, 'SELECT COUNT(*) FROM worker_results') == [(0,)]
            return {'scenario': 'model_429_primary_and_fallback_exhausted', 'passed': True, 'attempts': 2,
                    'automatic_switches': 1, 'active_failure_incidents': len(failures),
                    'requests': {PRIMARY: _requests(endpoint, primary, [429, 429]),
                                 FALLBACK: _requests(endpoint, fallback, [429, 429])}}


def _concurrent(root):
    paths = [worker_path(number=number) for number in (123, 124)]
    with model_http({path: [(429, RETRY_AFTER), (200, None)] for path in paths}) as endpoint:
        with worker_runtime(root, endpoint, tasks=2) as runtime:
            assert len(runtime.dispatch()) == 2
            runtime.finish()
            _assert_delivery(runtime, 2)
            # Independent clients need no fixed cross-worker arrival order.
            return {'scenario': 'model_429_concurrent_workers', 'passed': True, 'attempts': 2, 'results': 2,
                    'requests': {path: _requests(endpoint, path, [429, 200]) for path in paths}}


def _canceled(root):
    path = worker_path()
    with model_http({path: [(429, '10'), (200, None)]}) as endpoint:
        with worker_runtime(root, endpoint) as runtime:
            task_id, _, _, _, _ = runtime.attempts()[0]
            [(work_item_id, version)] = query(runtime.db, 'SELECT work_item_id,version FROM work_items')
            launched, = runtime.dispatch()
            wait_for(lambda: query(runtime.db, 'SELECT 1 FROM worker_phases WHERE summary=?',
                                   ('Waiting for model quota',)),
                     'worker consuming HTTP 429 and starting backoff')
            context = work_items.CommandContext('operator', 'operator', frozenset({REPO_ID}), frozenset())
            canceled = work_items.execute_command(runtime.db, work_item_id, context=context, command='cancel',
                                                  expected_version=version, idempotency_key='cancel', notifier=lambda _: None)
            assert canceled['ok'], canceled
            runtime.finish()
            runtime.observe()
            assert runtime.dispatch() == []
            assert runtime.attempts() == [(task_id, launched['attempt_id'], 'canceled', PRIMARY, None)]
            assert query(runtime.db, 'SELECT COUNT(*) FROM worker_results') == [(0,)]
            assert len(runtime.processes) == 1 and runtime.processes[0].returncode < 0
            return {'scenario': 'model_429_cancel_during_retry_after', 'passed': True, 'attempts': 1,
                    'worker_exit': runtime.processes[0].returncode, 'requests': _requests(endpoint, path, [429])}


def run(root):
    return [_recovers(root / 'model-429-once', 1), _recovers(root / 'model-429-repeated', 2),
            _exhausted(root / 'model-429-exhausted'), _concurrent(root / 'model-429-concurrent'),
            _canceled(root / 'model-429-canceled')]


if __name__ == '__main__':
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(prefix='robert-model-429-') as directory:
        print(json.dumps(run(Path(directory)), indent=2))
