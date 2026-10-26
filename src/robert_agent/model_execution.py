"""Shared supervised model stages for review and vote rounds."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sqlite3
import threading
import time

from robert_agent import dispatch, execution_store, review_files, review_sandbox, review_workspace, worker_identity
from robert_agent import review_store as store
from robert_agent.publication_lock import task_publication_lock
from robert_agent.worker import snapshot


def read_pr(repo, number):
    from robert_agent.review_coordinator import read_pr as read
    return read(repo, number)


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


def _active(conn, task_id, attempt_id):
    row = conn.execute('''SELECT t.lifecycle,a.status FROM tasks t JOIN attempts a ON a.task_id=t.task_id
        WHERE t.task_id=? AND a.attempt_id=? AND a.attempt_no=(
            SELECT MAX(attempt_no) FROM attempts WHERE task_id=t.task_id)''', (task_id, attempt_id)).fetchone()
    binding = execution_store.get_binding(conn, 'task', task_id)
    return bool(row and row[0] in {'queued', 'running'} and row[1] in {'prepared', 'running', 'stale'}
                and binding and binding['binding_status'] == 'active')


def terminate_children(conn, task_id, store=store):
    """Cancel all model process groups before allowing another attempt to start."""
    review = store.session(conn, task_id=task_id)
    if not review:
        return 'already_dead'
    outcomes = []
    updates = []
    for run in store.runs(conn, review['round_id']):
        if run['status'] in {'prepared', 'waiting'} and not run['slot_reserved']:
            updates.append((run['run_id'], {'status': 'interrupted', 'error': 'coordinator stopped before launch'}))
            continue
        if run['status'] not in {'starting', 'running', 'failed'}:
            continue
        recorded = run['dispatch']
        if run['status'] == 'failed' and not recorded.get('pid'):
            continue
        if _group_finished(recorded):
            updates.append((run['run_id'], {'slot_reserved': 0}))
            outcomes.append('already_dead')
            continue
        if recorded.get('process_exited') or (recorded and worker_identity.inspect_recorded_identity(recorded) == 'dead'):
            exited = _wait_for_group_exit(recorded)
            if exited:
                updates.append((run['run_id'], {'slot_reserved': 0}))
            outcomes.append('already_dead' if exited else 'attention')
            continue
        if not recorded:
            outcomes.append('attention')
            continue
        result = worker_identity.terminate_recorded_identity(recorded)
        if result['status'] != 'attention' and not _wait_for_group_exit(recorded):
            result = {'status': 'attention'}
        outcomes.append(result['status'])
        if result['status'] != 'attention':
            updates.append((run['run_id'], {'status': 'interrupted', 'slot_reserved': 0, 'error': 'coordinator stopped'}))
    # Observe/stop every child before opening our write transaction. In
    # particular, stopping the second child must not hold the first's update.
    for run_id, fields in updates:
        store.update_run(conn, run_id, **fields)
    return 'attention' if 'attention' in outcomes else 'terminated'


class StageCoordinator:
    store = store
    workspace = review_workspace
    voter_stage = 'review'
    handoff_files: dict[str, tuple[str, ...]] = {}
    artifact_kinds = ('result', 'report', 'evaluation', 'context_summary', 'source_index')
    workflow_name = 'review'

    def __init__(self, db_path, task_id, attempt_id, *, fetch_pr=read_pr,
                 launcher=dispatch.dispatch_worker, sandbox=review_sandbox.launch_policy,
                 poll_seconds=1):
        store = self.store
        self.db_path, self.task_id, self.attempt_id = str(db_path), task_id, attempt_id
        self.fetch_pr, self.launcher, self.sandbox = fetch_pr, launcher, sandbox
        self.poll_seconds = poll_seconds
        self.stopping = threading.Event()
        self.processes = {}
        self._heartbeat_lock = threading.Lock()
        self._next_heartbeat_at = 0.0
        with store.connect(db_path) as conn:
            self.session = store.session(conn, task_id=task_id)
        if not self.session:
            raise ValueError('no registered review round for task')
        self.round_id = self.session['round_id']
        self.request = self.session['request']
        self.data_dir = Path(db_path).resolve().parent
        self.manifest = self.session['manifest']

    def check_active(self):
        store = self.store
        if self.stopping.is_set():
            raise ReviewStopped('review coordinator is stopping')
        with store.connect(self.db_path) as conn:
            if not _active(conn, self.task_id, self.attempt_id):
                raise ReviewStopped('review attempt is no longer current')
        # Ownership/cancellation is checked on every poll. The shared parent
        # heartbeat is diagnostic and must not turn a busy writer into a failed
        # model round or make every reviewer contend on the same UPDATE.
        now = time.monotonic()
        if now < self._next_heartbeat_at or not self._heartbeat_lock.acquire(blocking=False):
            return
        try:
            if now < self._next_heartbeat_at:
                return
            try:
                with closing(sqlite3.connect(self.db_path, timeout=0.1)) as conn, conn:
                    if not _active(conn, self.task_id, self.attempt_id):
                        raise ReviewStopped('review attempt is no longer current')
                    conn.execute('UPDATE attempts SET heartbeat_at=? WHERE attempt_id=?',
                                 (store.now(), self.attempt_id))
            except sqlite3.OperationalError as exc:
                message = str(exc)
                if message not in {'database is locked', 'database table is locked',
                                   'database schema is locked'} and not message.startswith(
                        ('database table is locked:', 'database schema is locked:')):
                    raise
                self._next_heartbeat_at = now + 1
            else:
                self._next_heartbeat_at = now + 30
        finally:
            self._heartbeat_lock.release()

    def phase(self, status):
        store = self.store
        self.check_active()
        with store.connect(self.db_path) as conn:
            store.update(conn, self.round_id, status=status, error=None)
        snapshot.record_snapshot(self.db_path, self.task_id, self.attempt_id,
                                 'prepare' if status == 'preparing' else 'analyze', 'running',
                                 f'{self.workflow_name.title()} {self.round_id}: {status}')

    def save_manifest(self):
        store = self.store
        with store.connect(self.db_path) as conn:
            store.update(conn, self.round_id, manifest_json=json.dumps(self.manifest, ensure_ascii=False))
            conn.execute('UPDATE attempts SET worktree_path=? WHERE attempt_id=?',
                         (self.manifest['worktree_path'], self.attempt_id))

    def reconcile_running(self):
        """Finish previous model processes before scheduling any new phase."""
        store = self.store
        with store.connect(self.db_path) as conn:
            previous = store.runs(conn, self.round_id)
        workers = {worker['name']: worker for worker in self.request['workers']}
        pending = []
        for run in previous:
            if run['status'] == 'starting' and not run['dispatch']:
                raise ReviewStopped('model launch outcome unknown; inspect before retry')
            if run['status'] == 'running':
                pending.append(run)

        def reconcile(run):
            try:
                self._wait(run, run['dispatch'], workers[run['worker_id']]['timeout_seconds'])
            except ReviewStopped:
                raise
            except RuntimeError as exc:
                with store.connect(self.db_path) as conn:
                    store.update_run(conn, run['run_id'], status='failed', error=str(exc))
        self.parallel(pending, reconcile)

    def parallel(self, items, operation):
        """Stop sibling waiters on cancellation without losing running children."""
        if not items:
            return []
        with ThreadPoolExecutor(max_workers=len(items)) as executor:
            futures = {}
            try:
                for index, item in enumerate(items):
                    futures[executor.submit(operation, item)] = index
                results = [None] * len(items)
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
            except BaseException:
                self.stopping.set()
                for future in futures:
                    future.cancel()
                raise
        return results

    def release_slot(self, run):
        store = self.store
        with store.connect(self.db_path) as conn:
            store.release_slot(conn, run['run_id'])

    def reclaim_finished_slots(self):
        # A shared pool must reclaim proven-dead groups from either workflow;
        # unknown launches retain their reservation until explicit intervention.
        from robert_agent.native_rounds import STORES
        reserved = []
        with self.store.connect(self.db_path) as conn:
            for run_store, table in zip(STORES, ('review_runs', 'vote_runs')):
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                    reserved.extend((run_store, run) for run in run_store.reserved_runs(conn))
        for run_store, run in reserved:
            if run['dispatch'] and _group_finished(run['dispatch']):
                with self.store.connect(self.db_path) as conn:
                    run_store.release_finished_slot(conn, run)

    def launch_with_slot(self, run, worker, prompt_path, prefix, environment):
        store = self.store
        while True:
            self.check_active()
            self.reclaim_finished_slots()
            # Cancellation takes this same lock before changing the owner or
            # terminating children. Keep reservation/spawn/PID registration
            # together, but never retain a SQLite writer across external I/O.
            with task_publication_lock(self.db_path, self.task_id) as locked:
                if locked:
                    self.check_active()
                    with store.connect(self.db_path) as conn:
                        conn.execute('BEGIN IMMEDIATE')
                        if not _active(conn, self.task_id, self.attempt_id):
                            raise ReviewStopped('review attempt is no longer current')
                        acquired = store.reserve_slot(conn, run['run_id'])
                    if acquired:
                        launched = self.launcher(
                            task_id=self.task_id, attempt_id=run['run_id'], prompt_path=prompt_path,
                            worktree_path=self.manifest['worktree_path'], worker_agent_config=worker,
                            model=run['model'], reasoning_effort=worker['default_effort'],
                            trusted_environment=environment, command_prefix=prefix, log_dir=run['output_dir'], dry_run=False,
                        )
                        # Retain our Popen even when another thread's dispatch
                        # reaps the shared registry before this waiter polls it.
                        process = dispatch._BACKGROUND_PROCESSES.get(launched.get('pid'))
                        if process is not None:
                            self.processes[launched['pid']] = process
                        with store.connect(self.db_path) as conn:
                            store.update_run(conn, run['run_id'], status='running' if launched.get('ok') or launched.get('pid') else 'failed',
                                             dispatch_json=json.dumps(launched))
                            fresh = next(r for r in store.runs(conn, self.round_id) if r['run_id'] == run['run_id'])
                        run.update(fresh)
                        return launched
            self.stopping.wait(self.poll_seconds)

    def register(self, run, artifact):
        store = self.store
        self.check_active()
        with store.connect(self.db_path) as conn:
            store.update_run(conn, run['run_id'], status='succeeded', artifact_json=json.dumps(artifact), error=None)
            for kind in self.artifact_kinds:
                path = artifact.get(kind + '_path')
                if not path:
                    continue
                conn.execute('''INSERT OR IGNORE INTO artifacts(artifact_id,task_id,attempt_id,
                    artifact_type,path,sha256,bytes,created_at) VALUES (?,?,?,?,?,?,?,?)''',
                    (f"{run['run_id']}:{kind}", self.task_id, self.attempt_id, f'{self.workflow_name}_{kind}',
                     path, artifact[kind + '_sha256'], Path(path).stat().st_size, store.now()))
        return {**artifact, 'run_id': run['run_id'], 'worker_id': run['worker_id']}

    def sealed_output(self, run):
        """Copy only the handoff files outside the producer's writable sandbox.

        A detached descendant is not necessarily in the registered process
        group. It can no longer mutate the accepted inputs through its output
        directory. The marker also recovers a seal committed before SQLite.
        """
        store = self.store
        names = self.handoff_files[run['stage']]
        parent = 'runs' if run['stage'] == self.voter_stage else run['stage']
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
        store = self.store
        started = datetime.fromisoformat(run.get('started_at') or run['created_at'])
        while not launched.get('process_exited'):
            self.check_active()
            proc = self.processes.get(launched.get('pid')) or dispatch._BACKGROUND_PROCESSES.get(launched.get('pid'))
            if proc is not None:
                code = proc.poll()
                if code is not None:
                    dispatch._BACKGROUND_PROCESSES.pop(proc.pid, None)
                    self.processes.pop(proc.pid, None)
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
                self.release_slot(run)
                raise RuntimeError('review worker timed out')
            time.sleep(self.poll_seconds)
        with store.connect(self.db_path) as conn:
            store.update_run(conn, run['run_id'], dispatch_json=json.dumps(launched))
        if not _wait_for_group_exit(launched):
            raise ReviewStopped('review leader exited but process group remains; inspect and stop remaining processes before retry')
        self.release_slot(run)
        if launched.get('returncode', 0) != 0:
            raise RuntimeError(f"review worker exited with code {launched['returncode']}")

    def run_phase(self, stage, worker, prompt_path, validate, *, readable_files=(), input_hash=None):
        """Adopt a complete unregistered file, or run only this failed stage."""
        store = self.store
        self.workspace.verify_snapshot(self.manifest)
        prompt_hash = store.digest(prompt_path)
        if stage == self.voter_stage and prompt_hash != self.manifest['prompt_sha256']:
            raise ValueError('review prompt hash mismatch')
        with store.connect(self.db_path) as conn:
            prior = [r for r in store.runs(conn, self.round_id)
                     if r['stage'] == stage and r['worker_id'] == worker['name']]
        for run in reversed(prior):
            if run['status'] in {'prepared', 'waiting'} and not run['slot_reserved']:
                with store.connect(self.db_path) as conn:
                    store.update_run(conn, run['run_id'], status='interrupted', error='recovered before launch')
                continue
            if run['prompt_sha256'] != prompt_hash:
                if stage == 'synthesis' or (stage == 'prepare' and not self.manifest.get('frozen')):
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
                self.workspace.verify_snapshot(self.manifest)
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
            store.update_run(conn, run['run_id'], status='waiting')
        try:
            prefix, environment = self.sandbox(self.manifest, run['output_dir'], self.data_dir,
                                               prompt_path, readable_files, worker_agent=worker['agent'])
            launched = self.launch_with_slot(run, worker, prompt_path, prefix, environment)
            if not launched.get('ok'):
                if launched.get('pid') and not _wait_for_group_exit(launched):
                    raise ReviewStopped('failed model launch left a process group; inspect remaining processes')
                self.release_slot(run)
                raise RuntimeError(launched.get('safe_error', 'review launch failed'))
            self._wait(run, launched, worker['timeout_seconds'])
            if store.digest(prompt_path) != prompt_hash:
                raise ValueError('model changed prompt file')
            self.workspace.verify_snapshot(self.manifest)
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
