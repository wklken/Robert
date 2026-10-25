"""Native, restartable PR review coordinator, supervised as a normal attempt.

Models only read inputs and write their own output files. This trusted process
owns SQLite, phase transitions, result validation and the audited publication
proposal. One model at a time uses the task's existing concurrency slot.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import time

from robert_agent import dispatch, execution_store, review_files, review_sandbox
from robert_agent import review_store as store
from robert_agent import review_workspace, worker_identity
from robert_agent.worker import result as worker_result, snapshot


class ReviewStopped(RuntimeError):
    pass


class ReviewSuperseded(RuntimeError):
    pass


def _group_finished(recorded):
    """A dead leader alone cannot prove its output files have stopped changing."""
    pid = recorded.get('pid')
    pgid = recorded.get('process_group_id', pid)
    if type(pid) is not int or pid <= 1 or pgid != pid:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _wait_for_group_exit(recorded, timeout=2):
    deadline = time.monotonic() + timeout
    while True:
        proc = dispatch._BACKGROUND_PROCESSES.get(recorded.get('pid'))
        if proc is not None and proc.poll() is not None:
            dispatch._BACKGROUND_PROCESSES.pop(proc.pid, None)
        if _group_finished(recorded):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def read_pr(repo, number):
    completed = subprocess.run(['gh', 'api', f'repos/{repo}/pulls/{number}'],
                               capture_output=True, text=True, check=True, timeout=60)
    value = json.loads(completed.stdout)
    if not isinstance(value, dict) or value.get('number') != number:
        raise ValueError('GitHub returned invalid PR metadata')
    return value


def same_revision(manifest, pr):
    return (pr.get('state') == 'open' and pr.get('head', {}).get('sha') == manifest['head_sha']
            and pr.get('base', {}).get('sha') == manifest['base_sha']
            and pr.get('base', {}).get('ref') == manifest['base_ref'])


def _active(conn, task_id, attempt_id):
    row = conn.execute('''SELECT t.lifecycle,a.status FROM tasks t JOIN attempts a ON a.task_id=t.task_id
        WHERE t.task_id=? AND a.attempt_id=? AND a.attempt_no=(
            SELECT MAX(attempt_no) FROM attempts WHERE task_id=t.task_id)''', (task_id, attempt_id)).fetchone()
    binding = execution_store.get_binding(conn, 'task', task_id)
    return bool(row and row[0] in {'queued', 'running'} and row[1] in {'prepared', 'running', 'stale'}
                and binding and binding['binding_status'] == 'active')


def terminate_children(conn, task_id):
    """Cancel all model process groups before allowing another attempt to start."""
    review = store.session(conn, task_id=task_id)
    if not review:
        return 'already_dead'
    outcomes = []
    for run in store.runs(conn, review['round_id']):
        if run['status'] not in {'starting', 'running', 'failed'}:
            continue
        recorded = run['dispatch']
        if run['status'] == 'failed' and not recorded.get('pid'):
            continue
        if _group_finished(recorded):
            outcomes.append('already_dead')
            continue
        if recorded.get('process_exited') or (recorded and worker_identity.inspect_recorded_identity(recorded) == 'dead'):
            outcomes.append('already_dead' if _wait_for_group_exit(recorded) else 'attention')
            continue
        if not recorded:
            outcomes.append('attention')
            continue
        result = worker_identity.terminate_recorded_identity(recorded)
        if result['status'] != 'attention' and not _wait_for_group_exit(recorded):
            result = {'status': 'attention'}
        outcomes.append(result['status'])
        if result['status'] != 'attention':
            store.update_run(conn, run['run_id'], status='interrupted', error='coordinator stopped')
    return 'attention' if 'attention' in outcomes else 'terminated'


class Coordinator:
    def __init__(self, db_path, task_id, attempt_id, *, fetch_pr=read_pr,
                 launcher=dispatch.dispatch_worker, sandbox=review_sandbox.launch_policy,
                 poll_seconds=1):
        self.db_path, self.task_id, self.attempt_id = str(db_path), task_id, attempt_id
        self.fetch_pr, self.launcher, self.sandbox = fetch_pr, launcher, sandbox
        self.poll_seconds = poll_seconds
        with store.connect(db_path) as conn:
            self.session = store.session(conn, task_id=task_id)
        if not self.session:
            raise ValueError('no registered review round for task')
        self.round_id = self.session['round_id']
        self.request = self.session['request']
        self.data_dir = Path(db_path).resolve().parent
        self.manifest = self.session['manifest']

    def check_active(self):
        with store.connect(self.db_path) as conn:
            if not _active(conn, self.task_id, self.attempt_id):
                raise ReviewStopped('review attempt is no longer current')
            conn.execute('UPDATE attempts SET heartbeat_at=? WHERE attempt_id=?', (store.now(), self.attempt_id))

    def phase(self, status):
        self.check_active()
        with store.connect(self.db_path) as conn:
            store.update(conn, self.round_id, status=status, error=None)
        snapshot.record_snapshot(self.db_path, self.task_id, self.attempt_id,
                                 'prepare' if status == 'preparing' else 'analyze', 'running',
                                 f'Review {self.round_id}: {status}')

    def save_manifest(self):
        with store.connect(self.db_path) as conn:
            store.update(conn, self.round_id, manifest_json=json.dumps(self.manifest, ensure_ascii=False))
            conn.execute('UPDATE attempts SET worktree_path=? WHERE attempt_id=?',
                         (self.manifest['worktree_path'], self.attempt_id))

    def reconcile_running(self):
        """Finish previous model processes before scheduling any new phase."""
        with store.connect(self.db_path) as conn:
            previous = store.runs(conn, self.round_id)
        workers = {worker['name']: worker for worker in self.request['workers']}
        for run in previous:
            if run['status'] == 'starting' and not run['dispatch']:
                raise ReviewStopped('model launch outcome unknown; inspect before retry')
            if run['status'] != 'running':
                continue
            try:
                self._wait(run, run['dispatch'], workers[run['worker_id']]['timeout_seconds'])
            except ReviewStopped:
                raise
            except RuntimeError as exc:
                with store.connect(self.db_path) as conn:
                    store.update_run(conn, run['run_id'], status='failed', error=str(exc))

    def register(self, run, artifact):
        self.check_active()
        with store.connect(self.db_path) as conn:
            store.update_run(conn, run['run_id'], status='succeeded', artifact_json=json.dumps(artifact), error=None)
            for kind in ('result', 'report', 'evaluation', 'context_summary', 'source_index'):
                path = artifact.get(kind + '_path')
                if not path:
                    continue
                conn.execute('''INSERT OR IGNORE INTO artifacts(artifact_id,task_id,attempt_id,
                    artifact_type,path,sha256,bytes,created_at) VALUES (?,?,?,?,?,?,?,?)''',
                    (f"{run['run_id']}:{kind}", self.task_id, self.attempt_id, f'review_{kind}',
                     path, artifact[kind + '_sha256'], Path(path).stat().st_size, store.now()))
        return {**artifact, 'run_id': run['run_id'], 'worker_id': run['worker_id']}

    def sealed_output(self, run):
        """Copy only the handoff files outside the producer's writable sandbox.

        A detached descendant is not necessarily in the registered process
        group. It can no longer mutate the accepted inputs through its output
        directory. The marker also recovers a seal committed before SQLite.
        """
        names = {'prepare': ('context-summary.md', 'source-index.json'),
                 'review': ('result.json',), 'synthesis': ('evaluation.json',)}[run['stage']]
        parent = 'runs' if run['stage'] == 'review' else run['stage']
        sealed = Path(self.manifest['artifact_root']) / parent / 'sealed' / run['run_id']
        review_files.safe_path(sealed, self.manifest['artifact_root'])
        sealed.mkdir(parents=True, exist_ok=True)
        marker = sealed / 'seal.json'
        identity = {'run_id': run['run_id'], 'prompt_sha256': run['prompt_sha256']}
        if not marker.exists():
            hashes = {}
            output = review_files.safe_path(run['output_dir'], self.manifest['artifact_root'])
            directory_fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for name in names:
                    # Anchor the open directory too: replacing the producer
                    # directory with a symlink cannot redirect this read.
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
                    with os.fdopen(fd, 'rb') as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            raise ValueError('review handoff must be a regular file')
                        raw = stream.read(16 * 1024 * 1024 + 1)
                    if len(raw) > 16 * 1024 * 1024:
                        raise ValueError('review handoff exceeds 16 MiB')
                    temporary = sealed / (name + '.tmp')
                    temporary.write_bytes(raw)
                    temporary.replace(sealed / name)
                    hashes[name] = hashlib.sha256(raw).hexdigest()
            finally:
                os.close(directory_fd)
            review_files.atomic_json(marker, {**identity, 'files': hashes})
        saved = json.loads(marker.read_text(encoding='utf-8'))
        if any(saved.get(key) != value for key, value in identity.items()) or set(saved.get('files', {})) != set(names):
            raise ValueError('review seal identity changed')
        for name, expected in saved['files'].items():
            path = review_files.safe_path(sealed / name, sealed)
            if store.digest(path) != expected:
                raise ValueError('sealed review artifact changed')
        return sealed

    def _wait(self, run, launched, timeout):
        started = datetime.fromisoformat(run['created_at'])
        while not launched.get('process_exited'):
            self.check_active()
            proc = dispatch._BACKGROUND_PROCESSES.get(launched.get('pid'))
            if proc is not None:
                code = proc.poll()
                if code is not None:
                    dispatch._BACKGROUND_PROCESSES.pop(proc.pid, None)
                    launched = {**launched, 'process_exited': True, 'returncode': code}
                    break
            else:
                identity = worker_identity.inspect_recorded_identity(launched)
                if identity == 'dead':
                    # The coordinator restarted after the model finished. Its
                    # complete expected file is the durable completion evidence.
                    launched = {**launched, 'process_exited': True}
                    break
                if identity != 'alive_same':
                    raise ReviewStopped('review worker identity requires attention')
            if (datetime.now(timezone.utc) - started).total_seconds() > timeout:
                termination = worker_identity.terminate_recorded_identity(launched)
                if termination['status'] == 'attention':
                    raise ReviewStopped('review worker timeout termination requires attention')
                if not _wait_for_group_exit(launched):
                    raise ReviewStopped('review worker timeout left a process group; inspect remaining processes')
                raise RuntimeError('review worker timed out')
            time.sleep(self.poll_seconds)
        with store.connect(self.db_path) as conn:
            store.update_run(conn, run['run_id'], dispatch_json=json.dumps(launched))
        if not _wait_for_group_exit(launched):
            raise ReviewStopped('review leader exited but process group remains; inspect and stop remaining processes before retry')
        if launched.get('returncode', 0) != 0:
            raise RuntimeError(f"review worker exited with code {launched['returncode']}")

    def run_phase(self, stage, worker, prompt_path, validate, *, readable_files=(), input_hash=None):
        """Adopt a complete unregistered file, or run only this failed stage."""
        review_workspace.verify_snapshot(self.manifest)
        prompt_hash = store.digest(prompt_path)
        if stage == 'review' and prompt_hash != self.manifest['prompt_sha256']:
            raise ValueError('review prompt hash mismatch')
        with store.connect(self.db_path) as conn:
            prior = [r for r in store.runs(conn, self.round_id)
                     if r['stage'] == stage and r['worker_id'] == worker['name']]
        for run in reversed(prior):
            if run['prompt_sha256'] != prompt_hash:
                if stage == 'synthesis':
                    if run['status'] in {'running', 'succeeded'}:
                        with store.connect(self.db_path) as conn:
                            store.update_run(conn, run['run_id'], status='superseded', error='synthesis input set changed')
                    continue
                raise ValueError('same-round prompt changed')
            if run['status'] == 'starting' and not run['dispatch']:
                raise ReviewStopped('model launch outcome unknown; cancel and inspect this round')
            if run['status'] not in {'succeeded', 'running'}:
                continue
            try:
                if run['status'] == 'running':
                    self._wait(run, run['dispatch'], worker['timeout_seconds'])
                model = run['model'] or worker['default_model']
                artifact = validate(self.sealed_output(run), model)
                artifact['model'] = model
                if input_hash:
                    artifact['input_sha256'] = input_hash
                if run['status'] == 'succeeded' and artifact != run['artifact']:
                    raise ValueError('registered review artifact changed')
                review_workspace.verify_snapshot(self.manifest)
                return self.register(run, artifact)
            except ReviewStopped:
                raise
            except (ValueError, OSError, RuntimeError) as exc:
                with store.connect(self.db_path) as conn:
                    store.update_run(conn, run['run_id'], status='failed', error=str(exc))
                break
        self.check_active()
        with store.connect(self.db_path) as conn:
            previous = [r for r in store.runs(conn, self.round_id)
                        if r['stage'] == stage and r['worker_id'] == worker['name']]
            model = worker['default_model']
            if worker.get('fallback_model') and len(previous) >= 2 and all(
                r['status'] == 'failed' and r['error'] == 'review worker timed out'
                for r in previous[-2:]
            ):
                model = worker['fallback_model']
            run = store.new_run(conn, self.round_id, stage, worker['name'], self.attempt_id,
                                self.manifest['artifact_root'], prompt_hash, model=model)
        try:
            prefix, environment = self.sandbox(self.manifest, run['output_dir'], self.data_dir,
                                               prompt_path, readable_files)
            with store.connect(self.db_path) as conn:
                store.update_run(conn, run['run_id'], status='starting')
            launched = self.launcher(
                task_id=self.task_id, attempt_id=run['run_id'], prompt_path=prompt_path,
                worktree_path=self.manifest['worktree_path'], worker_agent_config=worker,
                model=model, reasoning_effort=worker['default_effort'],
                trusted_environment=environment, command_prefix=prefix, log_dir=run['output_dir'], dry_run=False,
            )
            with store.connect(self.db_path) as conn:
                store.update_run(conn, run['run_id'], status='running' if launched.get('ok') or launched.get('pid') else 'failed',
                                 dispatch_json=json.dumps(launched))
            if not launched.get('ok'):
                if launched.get('pid') and not _wait_for_group_exit(launched):
                    raise ReviewStopped('failed model launch left a process group; inspect remaining processes')
                raise RuntimeError(launched.get('safe_error', 'review launch failed'))
            self._wait(run, launched, worker['timeout_seconds'])
            if store.digest(prompt_path) != prompt_hash:
                raise ValueError('model changed prompt file')
            review_workspace.verify_snapshot(self.manifest)
            artifact = validate(self.sealed_output(run), model)
            artifact['model'] = model
            if input_hash:
                artifact['input_sha256'] = input_hash
            return self.register(run, artifact)
        except ReviewStopped:
            raise
        except Exception as exc:
            with store.connect(self.db_path) as conn:
                current = next(r for r in store.runs(conn, self.round_id) if r['run_id'] == run['run_id'])
                if current['status'] == 'starting' and not current['dispatch']:
                    raise ReviewStopped('model launch outcome unknown; inspect before retry') from exc
                store.update_run(conn, run['run_id'], status='failed', error=str(exc))
            raise

    def prompt(self, name, text):
        path = Path(self.manifest['artifact_root']) / name
        if path.exists():
            if path.read_bytes() != text.encode('utf-8'):
                raise ValueError('registered phase prompt changed')
        else:
            path.write_text(text, encoding='utf-8')
        return path

    def prepare(self):
        self.phase('preparing')
        self.reconcile_running()
        repo = self.request['repo']
        if not self.manifest:
            # Fetch races are bounded; no models have started and no bundle is sealed.
            for retry in range(2):
                pr = self.fetch_pr(repo['full_name'], self.session['pr_number'])
                if pr.get('state') != 'open':
                    raise ReviewSuperseded('PR is closed')
                expected_author = self.request['event'].get('pr_author_login', '')
                if pr.get('user', {}).get('login', '').casefold() != expected_author.casefold():
                    raise ValueError('PR author does not match the authorized review request')
                try:
                    self.manifest = review_workspace.prepare_snapshot(
                        repo['repo_root'], repo['worktree_root'],
                        self.data_dir / 'artifacts' / 'reviews' / hashlib.sha256(repo['full_name'].encode()).hexdigest()[:16],
                        self.round_id, pr, [self.request['event']],
                    )
                    if not self.manifest.get('frozen'):
                        self.manifest['worker_config_sha256'] = hashlib.sha256(
                            json.dumps(self.request['workers'], sort_keys=True).encode()).hexdigest()
                        review_files.atomic_json(Path(self.manifest['context_dir']) / 'manifest.json', self.manifest)
                    break
                except ValueError as exc:
                    if retry or 'changed while fetching' not in str(exc):
                        raise
            self.save_manifest()
        review_workspace.verify_snapshot(self.manifest)
        if not self.manifest.get('frozen'):
            prompt = self.prompt('prepare-prompt.md', review_files.build_prepare_prompt(self.manifest))

            def validate(output, model):
                summary = review_files.safe_path(output / 'context-summary.md', output)
                index = review_files.safe_path(output / 'source-index.json', output)
                if not summary.read_text(encoding='utf-8').strip():
                    raise ValueError('primary context summary is empty')
                source_index = json.loads(index.read_text(encoding='utf-8'))
                if (not isinstance(source_index, dict) or not isinstance(source_index.get('files'), list)
                        or not isinstance(source_index.get('reading_notes'), str)
                        or not source_index['reading_notes'].strip()):
                    raise ValueError('invalid primary source index')
                tree = Path(self.manifest['worktree_path'])
                for item in source_index['files']:
                    if not isinstance(item, dict) or not isinstance(item.get('reason'), str) or not item['reason'].strip():
                        raise ValueError('source index entry requires a reason')
                    name = review_files._source_path(item.get('path'))
                    if not review_files.safe_path(tree / name, tree).is_file():
                        raise ValueError('source index entry references missing source')
                return {'context_summary_path': str(summary), 'context_summary_sha256': store.digest(summary),
                        'source_index_path': str(index), 'source_index_sha256': store.digest(index)}

            result = self.run_phase('prepare', self.request['workers'][0], prompt, validate)
            for kind, name in [('context_summary', 'context-summary.md'), ('source_index', 'source-index.json')]:
                shutil.copyfile(result[kind + '_path'], Path(self.manifest['context_dir']) / name)
            self.manifest = review_workspace.freeze_context(self.manifest)
            self.save_manifest()
        if not same_revision(self.manifest, self.fetch_pr(repo['full_name'], self.session['pr_number'])):
            raise ReviewSuperseded('PR head/base changed after context preparation')

    def review(self):
        self.phase('reviewing')
        records, failures = [], []
        for worker in self.request['workers']:
            def validate(output, model, worker=worker):
                result_path = output / 'result.json'
                result = review_files.read_result(result_path, self.manifest)
                report_path = review_files.safe_path(output / 'report.md', output)
                report = review_files.render_individual(result, worker['name'], model)
                if report_path.exists() and report_path.read_text(encoding='utf-8') != report:
                    raise ValueError('derived review report changed')
                report_path.write_text(report, encoding='utf-8')
                return {'model': model, 'result_path': str(result_path),
                        'result_sha256': store.digest(result_path), 'report_path': str(report_path),
                        'report_sha256': store.digest(report_path)}
            try:
                records.append(self.run_phase('review', worker, self.manifest['prompt_path'], validate))
            except ReviewStopped:
                raise
            except (ValueError, OSError, RuntimeError) as exc:
                failures.append(f"{worker['name']}: {exc}")
        if failures:
            raise RuntimeError('; '.join(failures))
        return records

    def synthesize(self, records):
        self.phase('synthesizing')
        inputs = review_files.write_synthesis_inputs(self.manifest, records)
        prompt = self.prompt(f"synthesis-prompt-{inputs['sha256']}.md",
                             review_files.build_synthesis_prompt(self.manifest, inputs['path']))

        def validate(output, model):
            if store.digest(inputs['path']) != inputs['sha256']:
                raise ValueError('synthesis inputs changed')
            path = output / 'evaluation.json'
            review_files.read_evaluation(path, self.manifest, records)
            return {'evaluation_path': str(path), 'evaluation_sha256': store.digest(path)}

        result = self.run_phase('synthesis', self.request['workers'][0], prompt, validate,
                                readable_files=[inputs['path'], *[record[kind + '_path']
                                    for record in records for kind in ('result', 'report')]],
                                input_hash=inputs['sha256'])
        evaluation = review_files.read_evaluation(result['evaluation_path'], self.manifest, records)
        body = review_files.render_merged(self.manifest, records, evaluation)
        body += f'\n<!-- robert-review round={self.round_id} -->\n'
        path = Path(self.manifest['artifact_root']) / 'synthesis' / 'merged-review.md'
        path.write_text(body, encoding='utf-8')
        review_workspace.verify_snapshot(self.manifest)
        self.check_active()
        with store.connect(self.db_path) as conn:
            store.update(conn, self.round_id, status='ready', report_path=str(path), report_sha256=store.digest(path))
            conn.execute('''INSERT OR REPLACE INTO artifacts(artifact_id,task_id,attempt_id,
                artifact_type,path,sha256,bytes,created_at) VALUES (?,?,?,?,?,?,?,?)''',
                (self.round_id + ':merged', self.task_id, self.attempt_id, 'review_report', str(path),
                 store.digest(path), path.stat().st_size, store.now()))
        action = {'type': 'submit_pr_review', 'target_url': f"https://github.com/{self.session['repo']}/pull/{self.session['pr_number']}",
                  'body': body, 'round_id': self.round_id, 'commit_id': self.manifest['head_sha'],
                  'base_sha': self.manifest['base_sha'], 'base_ref': self.manifest['base_ref'],
                  'report_path': str(path), 'report_sha256': store.digest(path)}
        payload = worker_result.build_result(self.task_id, self.attempt_id, 'multi_agent_review_report',
                    [self.session['trigger_fingerprint']], [], f'Review report: {path}', [action], used_skills=[])
        recorded = worker_result.record_result(self.db_path, payload)
        if not recorded.get('ok'):
            raise RuntimeError('review result was rejected: ' + recorded.get('status', 'unknown'))
        return path

    def run(self):
        try:
            self.prepare()
            return self.synthesize(self.review())
        except Exception as exc:
            with store.connect(self.db_path) as conn:
                status = 'superseded' if isinstance(exc, ReviewSuperseded) else 'failed'
                if isinstance(exc, ReviewStopped):
                    status = 'canceled' if not _active(conn, self.task_id, self.attempt_id) else 'attention'
                store.update(conn, self.round_id, status=status, error=str(exc))
                terminate_children(conn, self.task_id)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--attempt-id', required=True)
    args = parser.parse_args(argv)

    def stop(_signum, _frame):
        raise ReviewStopped('coordinator received termination signal')
    signal.signal(signal.SIGTERM, stop)
    Coordinator(args.db, args.task_id, args.attempt_id).run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
