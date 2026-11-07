"""Shared fixtures for scheduler remediation tests."""
from robert_agent.adapters.processes import identity as worker_identity
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.adapters.sqlite import execution_control as execution_store
from robert_agent.runtime import loop as loop_engine
from robert_agent.adapters.sqlite import database as storage


class SchedulerRemediationFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / '.git').mkdir()
        self.config = self.root / 'config.yml'
        self.data = self.root / 'data'
        self.config.write_text(f"""version: 2
data_dir: {self.data}
database: runtime.sqlite3
github:
  account: robot
default_worker: default
workers:
  default:
    adapter: cbc
    command: cbc
    model: gpt-5.4
    effort: high
scheduler:
  max_concurrency: 3
supervision:
  hard_timeout_seconds: 5400
repos:
- full_name: example/repo
  github_account: robot
  trusted_actors:
  - maintainer
  checkout: {self.repo}
  worktrees: {self.repo}/.worktrees
  default_branch: main
""")
        self.db = self.data / 'runtime.sqlite3'
        storage.init_database(self.db)
        self.now = datetime.now(timezone.utc)
        self.process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        self.addCleanup(self.stop_process)
        time.sleep(.1)
        self.identity = worker_identity.capture_process_identity(self.process.pid)
        with self.connect() as conn:
            conn.execute("INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('repo:example/repo','example/repo','robot','main',?,?)", (str(self.repo), str(self.repo / '.worktrees')))
            conn.execute("INSERT INTO workstreams(workstream_id,repo_id,lifecycle,active_task_id,created_at,updated_at) VALUES ('w','repo:example/repo','active','t',?,?)", (self.now.isoformat(), self.now.isoformat()))
            conn.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,priority,created_at,updated_at) VALUES ('t','w','running','P1',?,?)", (self.now.isoformat(), self.now.isoformat()))
            conn.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status,started_at,heartbeat_at,metadata_json) VALUES ('a','t',1,'running',?,?,?)", (self.now.isoformat(), self.now.isoformat(), json.dumps({'dispatch': self.identity})))
            conn.execute("INSERT INTO worker_phases(phase_id,attempt_id,phase,status,summary,created_at) VALUES ('phase','a','analyze','running','Working',?)", (self.now.isoformat(),))
            execution_store.ensure_subject(conn, 'task', 't', 'human_request', self.now.isoformat())

    def connect(self):
        connection = sqlite3.connect(self.db)
        self.addCleanup(connection.close)
        return connection

    def stop_process(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)

    def runnable(self, **kwargs):
        with self.connect() as conn:
            return loop_engine.has_runnable_local_work(conn, repo_ids=['repo:example/repo'], **kwargs)

    def result(self):
        with self.connect() as conn:
            conn.execute("INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at) VALUES ('result','t','a','comment_analysis',?)", (self.now.isoformat(),))
            conn.execute("INSERT INTO wakeups(wakeup_id,repo_id,reason,dedupe_key,task_id,attempt_id,result_id,status,not_before_at,created_at,updated_at) VALUES ('wake','repo:example/repo','worker_result_ready','result','t','a','result','pending',?,?,?)", (self.now.isoformat(),) * 3)
