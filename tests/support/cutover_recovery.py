"""Shared fixtures for cutover recovery tests."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.runtime import daemon
from robert_agent.execution import runtime_state as daemon_state
from robert_agent.adapters.sqlite import database as storage


class CutoverRecoveryFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "db.sqlite"
        storage.init_database(self.db)
        self.now = "2026-10-31T00:00:00+00:00"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES('repo','x/y','bot','main','/repo','/trees')"
            )
            for name in ("bad", "good"):
                conn.execute(
                    "INSERT INTO workstreams(workstream_id,repo_id,lifecycle,active_task_id,created_at,updated_at) VALUES(?,'repo','active',?,?,?)",
                    (name, name, self.now, self.now),
                )
                conn.execute(
                    "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES(?,?,'queued',?,?)",
                    (name, name, self.now, self.now),
                )
                conn.execute(
                    "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,started_at,heartbeat_at,metadata_json) VALUES(?,?,1,'running',?,?,?)",
                    (
                        name,
                        name,
                        self.now,
                        self.now,
                        json.dumps(
                            {
                                "result_contract": "compact-v1",
                                "dispatch": {"pid": 123456789},
                            }
                        ),
                    ),
                )
            conn.execute(
                "INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at) VALUES('old','bad','bad','classification_result',?)",
                (self.now,),
            )
            conn.execute(
                "INSERT INTO work_items(work_item_id,repo_id,title,origin_type,workstream_id,creation_idempotency_key,created_by,created_at,updated_at) VALUES('item','repo','Bad request','web','bad','item','operator',?,?)",
                (self.now, self.now),
            )

    def context(self):
        with closing(daemon_state.connect(self.db)) as conn, conn:
            run = daemon_state.start_daemon_run(
                conn, config_path="config", owner_id="owner", now=self.now
            )
        return daemon.DaemonContext(
            "config",
            None,
            self.db,
            "repo",
            3,
            {
                "local_poll_seconds": 5,
                "github_poll_seconds": 300,
                "local_drain_timeout_seconds": 180,
                "live_run_timeout_seconds": 180,
            },
            run["daemon_run_id"],
            "lease",
            repo_ids=["repo"],
        )

    def github_item(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number,html_url,state) VALUES('source','repo','github:x/y#205','issue',205,'https://github.com/x/y/issues/205','open')"
            )
            conn.execute(
                "UPDATE workstreams SET primary_source_id='source' WHERE workstream_id='bad'"
            )
            conn.execute(
                "UPDATE work_items SET origin_type='github',origin_source_id='source' WHERE work_item_id='item'"
            )
            conn.execute(
                "INSERT INTO github_events(event_id,repo_id,source_id,event_type,event_fingerprint,payload_json,event_at) VALUES('event','repo','source','assigned','assignment:205',?,?)",
                (json.dumps({"event_type": "assigned", "number": 205}), self.now),
            )
            conn.execute(
                "INSERT INTO task_events(task_id,event_id,relationship,created_at) VALUES('bad','event','trigger',?)",
                (self.now,),
            )
