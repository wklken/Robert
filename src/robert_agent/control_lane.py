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
from uuid import uuid4

from robert_agent import worker_identity


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
                    return {'ok': False, 'status': 'attention', 'safe_error': 'Control child cleanup unproven'}
                return {'ok': False, 'status': 'child_timeout'}
            return None
        from robert_agent.daemon import _process_group_stopped
        if not _process_group_stopped(self.process.pid):
            return {'ok': False, 'status': 'attention', 'safe_error': 'Control child descendants still active'}
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
        rows = conn.execute('''SELECT DISTINCT c.child_id,c.identity_json,c.status
            FROM github_control_children c JOIN leases l ON l.owner_id=c.owner_id
            WHERE c.status IN ('starting','running') AND c.owner_id!=?
              AND l.resource_type='daemon' AND l.resource_key=?
              AND (l.status!='active' OR julianday(l.expires_at)<=julianday('now'))''',
            (current_owner, scope_key)).fetchall()
    safe = True
    for identifier, encoded, status in rows:
        if status == 'starting':
            with closing(sqlite3.connect(db_path, timeout=5)) as conn, conn:
                canceled = conn.execute("UPDATE github_control_children SET status='stopped' WHERE child_id=? AND status='starting'", (identifier,)).rowcount
            if canceled:
                continue
            return False  # The wrapper claimed concurrently; reobserve later.
        recorded = json.loads(encoded)
        result = worker_identity.terminate_recorded_identity(recorded)
        from robert_agent.daemon import _process_group_stopped
        if result['status'] == 'attention' or not _process_group_stopped(recorded.get('pid', 0)):
            safe = False
            continue
        with closing(sqlite3.connect(db_path, timeout=5)) as conn, conn:
            conn.execute("UPDATE github_control_children SET status='stopped' WHERE child_id=?", (identifier,))
    return safe


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--child', action='store_true', required=True)
    parser.add_argument('--db', required=True)
    parser.add_argument('--id', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    identity = worker_identity.capture_process_identity(os.getpid())
    with closing(sqlite3.connect(args.db, timeout=5)) as conn, conn:
        claimed = conn.execute("UPDATE github_control_children SET status='running',identity_json=? WHERE child_id=? AND status='starting'",
            (json.dumps(identity), args.id)).rowcount
    if not claimed:
        return 3
    return subprocess.call(command)


if __name__ == '__main__':
    raise SystemExit(main())
