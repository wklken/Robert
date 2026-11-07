"""Shared fixtures for retention tests."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.runtime import daemon
from robert_agent.execution import runtime_state as daemon_state
from robert_agent.adapters.sqlite import database as storage
from tests.support import DAEMON_DEFAULTS, maintenance_config


NOW = '2026-10-23T00:00:00+00:00'


OLD = '2026-08-01T00:00:00+00:00'


class RetentionFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / 'state.sqlite3'
        storage.init_database(self.db)
        with self.connect() as conn:
            conn.execute("INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('repo','example/repo','bot','main','/repo','/worktrees')")
            daemon_state.start_daemon_run(conn, config_path='/config.yml', owner_id='daemon', now=OLD)
            self.lease = daemon_state.acquire_daemon_lease(conn, resource_key='database', owner_id='daemon', ttl_seconds=60, now=OLD)['lease_id']
            conn.execute("INSERT INTO workstreams(workstream_id,repo_id,lifecycle,created_at,updated_at) VALUES ('ws','repo','active',?,?)", (OLD, OLD))
            conn.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES ('task','ws','running',?,?)", (OLD, OLD))
            conn.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status,started_at) VALUES ('attempt','task',1,'running',?)", (OLD,))
            conn.execute("INSERT INTO audit_events(audit_id,task_id,event_type,created_at,payload_json) VALUES ('audit','task','accepted',?,'{}')", (OLD,))
        self.context = daemon.DaemonContext(config_path='/config.yml', workflow_path=None, db_path=self.db, repo_id='repo', max_concurrency=3, daemon_config=dict(DAEMON_DEFAULTS), daemon_run_id='daemon', lease_id=self.lease, runtime_config={'db_path': str(self.db), 'data_dir': str(self.db.parent), 'maintenance': maintenance_config()})

    def connect(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        self.addCleanup(conn.close)
        return conn

    def add_run(self, name, state='completed', when=OLD, finished=True):
        with self.connect() as conn:
            conn.execute('INSERT INTO agent_runs(run_id,status,started_at,finished_at) VALUES (?,?,?,?)', (name, state, when, when if finished else None))
            conn.execute("INSERT INTO run_steps(step_id,run_id,step_key,status) VALUES (?,?,'summarize','succeeded')", ('s-'+name, name))
            conn.execute("INSERT INTO run_repo_steps(step_id,run_id,repo_id,step_key,status) VALUES (?,?,'repo','route','succeeded')", ('rs-'+name, name))

    def add_event(self, name, state='ok', when=OLD):
        with self.connect() as conn:
            conn.execute('INSERT INTO daemon_events(daemon_event_id,daemon_run_id,event_type,status,created_at) VALUES (?,\'daemon\',\'poll\',?,?)', (name, state, when))

    def add_lease(self, name, state='released', owner='ended', when=OLD):
        with self.connect() as conn:
            conn.execute("INSERT INTO leases(lease_id,resource_type,resource_key,owner_id,status,acquired_at,expires_at,heartbeat_at,released_at) VALUES (?,'agent_run',?,?,?,?,?,?,?)", (name, name, owner, state, when, when, when, when))

    def core_rows(self):
        with self.connect() as conn:
            return {t: conn.execute('SELECT * FROM '+t).fetchall() for t in ('tasks','attempts','audit_events','wakeups','workstreams')}
