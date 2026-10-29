"""Owned asynchronous control children with a durable startup handshake."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from uuid import UUID, uuid4

from robert_agent import daemon_state, worker_identity
from robert_agent.common import emit


def _boot_context():
    """Read a boot token without changing global process-identity semantics."""
    try:
        if sys.platform.startswith('linux'):
            token = Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
            namespace = os.readlink('/proc/self/ns/pid')
            if token and namespace:
                return {'platform': 'linux', 'boot_id': str(UUID(token)),
                        'boot_source': 'linux_boot_id', 'pid_namespace': namespace}
        elif sys.platform == 'darwin':
            result = subprocess.run(['sysctl', '-n', 'kern.bootsessionuuid'], capture_output=True,
                                    text=True, timeout=2, check=False)
            if result.returncode == 0:
                return {'platform': 'darwin', 'boot_id': str(UUID(result.stdout.strip())),
                        'boot_source': 'macos_boot_session_uuid'}
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError):
        pass
    return None


def _different_boot(recorded, current):
    previous = recorded.get('execution_context')
    return bool(isinstance(previous, dict) and isinstance(current, dict)
                and previous.get('platform') in {'linux', 'darwin'}
                and previous.get('platform') == current.get('platform')
                and previous.get('boot_source') in {'linux_boot_id', 'macos_boot_session_uuid'}
                and previous.get('boot_source') == current.get('boot_source')
                and previous.get('boot_id') and current.get('boot_id')
                and previous['boot_id'] != current['boot_id'])


def _owner_lease_active(conn, owner):
    return bool(conn.execute("""SELECT 1 FROM leases WHERE owner_id=?
        AND resource_type='daemon' AND status='active'
        AND (julianday(expires_at) IS NULL OR julianday(expires_at)>julianday('now')) LIMIT 1""",
        (owner,)).fetchone())


def _retire(conn, child, *, resolution, current_context=None):
    identifier, owner, lane, encoded, status = child
    if status == 'stopped':
        return {'ok': True, 'status': 'already_stopped', 'child_id': identifier}
    if _owner_lease_active(conn, owner):
        raise ValueError('Cannot acknowledge a control child while its daemon owner lease is active')
    changed = conn.execute("UPDATE github_control_children SET status='stopped' WHERE child_id=? AND status IN ('starting','running')",
                           (identifier,)).rowcount
    if not changed:
        raise ValueError('Control child no longer has a recoverable status')
    recorded = json.loads(encoded)
    evidence = {'child_id': identifier, 'lane': lane, 'resolution': resolution,
                'previous_execution_context': recorded.get('execution_context'),
                'current_execution_context': current_context}
    if resolution == 'operator_acknowledged_stopped':
        evidence['acknowledged_by_uid'] = os.getuid()
    daemon_state.record_event(conn, owner, 'control_child_retired', 'ok', evidence)
    return {'ok': True, 'status': 'retired', 'child_id': identifier, 'resolution': resolution}


def list_children(db_path):
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        rows = conn.execute('SELECT child_id,owner_id,lane,identity_json,status FROM github_control_children ORDER BY owner_id,lane,child_id').fetchall()
        return {'ok': True, 'children': [
            {'child_id': identifier, 'owner_id': owner, 'lane': lane, 'status': status,
             'identity': json.loads(encoded), 'owner_lease_active': _owner_lease_active(conn, owner)}
            for identifier, owner, lane, encoded, status in rows]}


def acknowledge_stopped(db_path, identifier):
    """Record an operator assertion; this operation never signals a process."""
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=rw', uri=True, timeout=5)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        child = conn.execute('SELECT child_id,owner_id,lane,identity_json,status FROM github_control_children WHERE child_id=?', (identifier,)).fetchone()
        if child is None:
            raise ValueError('Control child record was not found')
        if not conn.execute('SELECT 1 FROM daemon_runs WHERE daemon_run_id=?', (child[1],)).fetchone():
            raise ValueError('Control child daemon owner record was not found')
        return _retire(conn, child, resolution='operator_acknowledged_stopped')


class ControlLane:
    def __init__(self, db_path, lane, owner):
        self.db_path, self.lane, self.owner = db_path, lane, owner
        self.process = None
        self.recorded = None
        self.started = None
        self.timeout = None
        self.output = None
        self.error = None
        self.identifier = None
        self.attention_reported = False

    def _state(self):
        with closing(sqlite3.connect(self.db_path, timeout=5)) as conn:
            row = conn.execute('SELECT status,identity_json FROM github_control_children WHERE child_id=?',
                               (self.identifier,)).fetchone()
        if row:
            self.recorded = json.loads(row[1]) or None
        return row[0] if row else None

    def start(self, command, *, timeout_seconds):
        if self.process is not None:
            return False
        self.attention_reported = False
        self.timeout = timeout_seconds
        self.started = time.monotonic()
        self.identifier = uuid4().hex
        # The wrapper cannot execute any remote work until it atomically saves
        # its own identity. Recovery can cancel an unclaimed starting row.
        with closing(sqlite3.connect(self.db_path, timeout=5)) as conn, conn:
            conn.execute('''INSERT INTO github_control_children
                (child_id,owner_id,lane,identity_json,status) VALUES (?,?,?,'{}','starting')''',
                (self.identifier, self.owner, self.lane))
        try:
            self.output = tempfile.TemporaryFile(mode='w+t')
            self.error = tempfile.TemporaryFile(mode='w+t')
            environment = {**os.environ, '_ROBERT_EXECUTOR_ID': self.identifier,
                '_ROBERT_CONTROL_DEADLINE': str(self.started + max(.1, timeout_seconds - 2))}
            wrapper = [sys.executable, '-B', str(Path(__file__).resolve()), '--child',
                       '--db', str(self.db_path), '--id', self.identifier, '--', *command]
            self.process = subprocess.Popen(wrapper, stdout=self.output, stderr=self.error,
                text=True, start_new_session=True, env=environment)
        except Exception:
            self._finish()
            raise
        return True

    def poll(self):
        if self.process is None:
            return None
        self._state()
        if self.process.poll() is None:
            if time.monotonic() - self.started >= self.timeout:
                if not self.stop():
                    return self._attention('Control child cleanup unproven')
                return {'ok': False, 'status': 'child_timeout'}
            return None
        from robert_agent.daemon import _process_group_stopped
        if not _process_group_stopped(self.process.pid):
            return self._attention('Control child descendants still active')
        self.output.seek(0)
        try:
            payload = json.loads(self.output.read(1024 * 1024))
            if not isinstance(payload, dict):
                raise ValueError('expected object')
        except ValueError:
            payload = {'ok': False, 'status': 'child_failed'}
        payload['ok'] = bool(payload.get('ok') and self.process.returncode == 0)
        self._finish()
        return payload

    def _attention(self, reason):
        if self.attention_reported:
            return None
        self.attention_reported = True
        return {'ok': False, 'status': 'attention', 'safe_error': reason}

    def _finish(self):
        with closing(sqlite3.connect(self.db_path, timeout=5)) as conn, conn:
            conn.execute("UPDATE github_control_children SET status='stopped' WHERE child_id=?", (self.identifier,))
            conn.execute("DELETE FROM github_control_children WHERE owner_id=? AND status='stopped' AND child_id!=?",
                         (self.owner, self.identifier))
        if self.output is not None:
            self.output.close()
        if self.error is not None:
            self.error.close()
        self.output = self.error = None
        self.process = None

    def stop(self):
        if self.process is None:
            return True
        if self.process.poll() is None:
            with closing(sqlite3.connect(self.db_path, timeout=5)) as conn, conn:
                canceled = conn.execute("UPDATE github_control_children SET status='stopped' WHERE child_id=? AND status='starting'",
                                        (self.identifier,)).rowcount
            self._state()
            if canceled:
                # The still-unreaped wrapper is our child and cannot recycle its
                # PID; the transaction forbids it from spawning the command.
                self.process.terminate()
            elif self.recorded:
                result = worker_identity.terminate_recorded_identity(self.recorded)
                if result['status'] == 'attention':
                    return False
            else:
                return False
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                return False
        from robert_agent.daemon import _process_group_stopped
        if not _process_group_stopped(self.process.pid):
            return False
        self._finish()
        return True


def recover(db_path, *, scope_key, current_owner):
    with closing(sqlite3.connect(db_path, timeout=5)) as conn:
        rows = conn.execute('''SELECT DISTINCT c.child_id,c.identity_json,c.status,c.owner_id,c.lane
            FROM github_control_children c JOIN leases l ON l.owner_id=c.owner_id
            WHERE c.status IN ('starting','running') AND c.owner_id!=?
              AND l.resource_type='daemon' AND l.resource_key=?
              AND (l.status!='active' OR julianday(l.expires_at)<=julianday('now'))''',
            (current_owner, scope_key)).fetchall()
    safe = True
    current_context = _boot_context() if rows else None
    for identifier, encoded, status, owner, lane in rows:
        if status == 'starting':
            with closing(sqlite3.connect(db_path, timeout=5)) as conn, conn:
                canceled = conn.execute("UPDATE github_control_children SET status='stopped' WHERE child_id=? AND status='starting'", (identifier,)).rowcount
            if canceled:
                continue
            return False  # The wrapper claimed concurrently; reobserve later.
        recorded = json.loads(encoded)
        if _different_boot(recorded, current_context):
            with closing(sqlite3.connect(db_path, timeout=5)) as conn, conn:
                conn.execute('BEGIN IMMEDIATE')
                try:
                    _retire(conn, (identifier, owner, lane, encoded, status),
                            resolution='different_boot', current_context=current_context)
                except ValueError:
                    safe = False
            continue
        result = worker_identity.terminate_recorded_identity(recorded)
        from robert_agent.daemon import _process_group_stopped
        if result['status'] == 'attention' or not _process_group_stopped(recorded.get('pid', 0)):
            safe = False
            continue
        with closing(sqlite3.connect(db_path, timeout=5)) as conn, conn:
            conn.execute("UPDATE github_control_children SET status='stopped' WHERE child_id=?", (identifier,))
    return safe


def main(argv=None):
    parser = argparse.ArgumentParser(description='Inspect or recover owned scanner/publisher children')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--child', action='store_true')
    mode.add_argument('--list', action='store_true', help='List child records and owner lease state without changing them')
    mode.add_argument('--acknowledge-stopped', action='store_true',
                      help='Assert the old execution context has been stopped; retire its record without signaling')
    parser.add_argument('--db', required=True)
    parser.add_argument('--id')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.list:
        return emit(list_children(args.db))
    if not args.id:
        parser.error('--id is required for child or acknowledgment mode')
    if args.acknowledge_stopped:
        try:
            result = acknowledge_stopped(args.db, args.id)
        except ValueError as exc:
            return emit({'ok': False, 'status': 'rejected', 'safe_error': str(exc)}, 3)
        return emit(result)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    identity = worker_identity.capture_process_identity(os.getpid())
    context = _boot_context()
    if context is not None:
        identity["execution_context"] = context
    with closing(sqlite3.connect(args.db, timeout=5)) as conn, conn:
        claimed = conn.execute("UPDATE github_control_children SET status='running',identity_json=? WHERE child_id=? AND status='starting'",
            (json.dumps(identity), args.id)).rowcount
    if not claimed:
        return 3
    return subprocess.call(command)


if __name__ == '__main__':
    raise SystemExit(main())
