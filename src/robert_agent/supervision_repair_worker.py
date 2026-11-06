"""Trusted result broker for a restricted, policy-selected repair agent.

The inner agent receives a prompt and returns a compact submission. Only this
outer process can call Robert's ordinary result validation and durable spool.
"""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

from robert_agent import supervision_git, supervision_repair, validate_config, worker_identity
from robert_agent.worker import result, snapshot


MAX_OUTPUT = 128 * 1024


def launch_profile(config, task_id, attempt_id, worker):
    """Keep repository modules out of the trusted outer broker's import path."""
    # The trusted broker rechecks controller credential paths at dispatch.
    # The inner worker's separate environment excludes these overrides.
    environment = sorted({'GH_CONFIG_DIR', 'XDG_CONFIG_HOME', *worker.get('environment_allowlist', [])})
    return {'agent': 'command', 'command_argv': [sys.executable, '-I', '-m',
        'robert_agent.supervision_repair_worker', '--config', str(Path(config['config_path']).resolve()),
        '--task-id', task_id, '--attempt-id', attempt_id],
        'timeout_seconds': worker['timeout_seconds'] + 30,
        'environment_allowlist': environment}


def _object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError('duplicate submission key')
    return value


def _live_children():
    result = subprocess.run(['ps', '-axo', 'pid=,pgid=,stat='], capture_output=True, text=True, timeout=1, start_new_session=True)
    if result.returncode:
        raise OSError('repair process group unavailable')
    return [int(fields[0]) for line in result.stdout.splitlines()
            if len(fields := line.split()) == 3 and fields[0].isdigit() and fields[1].isdigit()
            and int(fields[1]) == os.getpgrp() and int(fields[0]) != os.getpid() and fields[2][0] not in 'ZXx']


def _cleanup_group(proc):
    # dispatch creates a private group for the broker. Its TERM handler remains
    # installed while every descendant is stopped; PID identity is rechecked
    # before an individual KILL, since the group leader must remain to reap.
    try:
        if _live_children():
            os.killpg(os.getpgrp(), signal.SIGTERM)
            time.sleep(.2)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            remaining = _live_children()
            if not remaining:
                break
            for pid in remaining:
                try:
                    identity = worker_identity.capture_process_identity(pid)
                    if identity['process_group_id'] == os.getpgrp() and worker_identity.inspect_recorded_identity(identity) == 'alive_same':
                        os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, worker_identity.IdentityReadError):
                    pass
            time.sleep(.05)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=2)


def run(config, task_id, attempt_id, prompt):
    if os.getpgrp() != os.getpid():
        return {'ok': False, 'status': 'repair_broker_group_not_isolated'}
    with closing(sqlite3.connect(config['db_path'])) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('''SELECT w.repo_id,a.worktree_path,a.branch_name,a.metadata_json FROM attempts a JOIN tasks t ON t.task_id=a.task_id
            JOIN workstreams w ON w.workstream_id=t.workstream_id WHERE a.task_id=? AND a.attempt_id=?''',
            (task_id, attempt_id)).fetchone()
        if not row or not row['worktree_path']:
            return {'ok': False, 'status': 'repair_target_missing'}
        authorization = supervision_repair.authorized_repair(conn, task_id, config, row['repo_id'])
    if not authorization:
        return {'ok': False, 'status': 'repair_policy_rejected'}
    try:
        value = supervision_git.binding({
            'repair_git': json.loads(row['metadata_json']).get('repair_git'),
            'worktree_path': row['worktree_path'], 'branch_name': row['branch_name'],
            'repo': authorization['repo']['full_name'],
        })
        if Path(value['private_root']) != Path(config['data_dir']).resolve():
            raise OSError('Invalid private Git view location')
        supervision_git.require_standalone(value['worktree'])
    except (OSError, ValueError):
        return {'ok': False, 'status': 'repair_git_binding_unavailable'}
    probed = supervision_repair.probe_restricted_launcher(config, row['worktree_path'], authorization=authorization)
    if not probed['ok']:
        return probed
    stop_requested = False
    def stop(_signal, _frame):
        nonlocal stop_requested
        stop_requested = True
    handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    proc = None
    try:
        with tempfile.TemporaryDirectory(prefix='robert-repair-model-') as home:
            environment = supervision_repair.repair_environment(authorization, home)
            packet = {'prompt': prompt, 'result_transport': 'stdout_compact_submission',
                      'instructions': 'Use only the prepared workspace. Do not access Robert DB or runtime commands. '
                        'Return exactly one compact-v1 new_pr submission JSON object on stdout. '
                        'The trusted broker binds task/attempt/output identity and submits it for ordinary audit. '
                        'Do not push, open PRs, merge, deploy or change the original task. '
                        'Describe failed verification honestly; do not claim a deployed fix.'}
            with tempfile.TemporaryFile(mode='w+b') as stdin:
                stdin.write(json.dumps(packet).encode())
                stdin.seek(0)
                # Stay in the outer worker's process group: controller cancellation
                # and identity-based cleanup must include every restricted child.
                proc = subprocess.Popen(supervision_repair.repair_command(authorization), cwd=row['worktree_path'],
                    env=environment, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            output = bytearray()
            deadline = time.monotonic() + authorization['worker']['timeout_seconds']
            heartbeat_due = 0
            with selectors.DefaultSelector() as reader:
                os.set_blocking(proc.stdout.fileno(), False)
                reader.register(proc.stdout, selectors.EVENT_READ)
                eof = False
                while not stop_requested:
                    now = time.monotonic()
                    if now >= deadline:
                        return {'ok': False, 'status': 'repair_worker_timeout'}
                    if now >= heartbeat_due:
                        try:
                            snapshot.record_snapshot(config['db_path'], task_id, attempt_id, 'execute',
                                                     'running', 'Restricted repair agent executing')
                        except sqlite3.Error:
                            pass
                        heartbeat_due = now + 10
                    if eof:
                        if proc.poll() is not None:
                            break
                        time.sleep(.05)
                    elif reader.select(.1):
                        chunk = os.read(proc.stdout.fileno(), min(8192, MAX_OUTPUT + 1 - len(output)))
                        if chunk:
                            output.extend(chunk)
                            if len(output) > MAX_OUTPUT:
                                return {'ok': False, 'status': 'repair_result_too_large'}
                        else:
                            eof = True
            if stop_requested:
                return {'ok': False, 'status': 'repair_worker_stopped'}
            if proc.returncode != 0:
                return {'ok': False, 'status': 'repair_worker_failed'}
            if _live_children():
                return {'ok': False, 'status': 'repair_worker_children_alive'}
            submission = json.loads(output, object_pairs_hook=_object)
            if not isinstance(submission, dict):
                return {'ok': False, 'status': 'invalid_repair_submission'}
            # Policy may be revoked while the model runs. Never submit under a
            # stale profile; ordinary result validation rechecks task ownership.
            current = validate_config.validate_config(config['config_path'], skip_external=True)
            if not current.get('ok'):
                return {'ok': False, 'status': 'repair_policy_rejected'}
            with closing(sqlite3.connect(config['db_path'])) as conn:
                conn.row_factory = sqlite3.Row
                if not supervision_repair.authorized_repair(conn, task_id, current, row['repo_id']):
                    return {'ok': False, 'status': 'repair_policy_rejected'}
            return result.record_result(config['db_path'], {'task_id': task_id, 'attempt_id': attempt_id,
                                                          'output_type': 'new_pr', 'submission': submission})
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
        return {'ok': False, 'status': 'repair_broker_failed'}
    finally:
        if proc is not None:
            _cleanup_group(proc)
            proc.stdout.close()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--attempt-id', required=True)
    args = parser.parse_args()
    config = validate_config.validate_config(args.config, skip_external=True)
    response = run(config, args.task_id, args.attempt_id, sys.stdin.read()) if config.get('ok') else config
    print(json.dumps(response))
    return 0 if response.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
