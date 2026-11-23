"""Regression contracts for bounded recovery and durable user outcomes."""

from contextlib import closing

from datetime import datetime, timezone, timedelta

import json

from pathlib import Path

import sqlite3

import tempfile

from types import SimpleNamespace

import unittest

from unittest import mock

from tests.support import PACKAGE_ROOT  # noqa: F401

from robert_agent.persistence.schema import database as storage

from robert_agent.delivery import values as publication_values

from robert_agent.delivery.application import publish


class PublicationBudgetFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "db"
        self.now = datetime.now(timezone.utc)
        self.calls = []
        self.error = "permission denied"
        storage.init_database(self.db)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('repo','x/y','bot','main','/repo','/worktrees')"
            )
            c.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,lifecycle,created_at,updated_at) VALUES ('ws','repo','active','now','now')"
            )
            c.execute(
                "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES ('t','ws','running','now','now')"
            )
            c.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json) VALUES ('a','t',1,'completed',json_object('result_contract','compact-v1'))"
            )
            scope = {
                "repo": "x/y",
                "worktree_path": self.tmp.name,
                "branch_name": "codex/fix",
                "remote": "origin",
                "result_head_sha": "b" * 40,
                "sources": [{"source_type": "issue", "number": 1}],
            }
            c.execute(
                "INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json) VALUES ('r','t','a','new_pr','now',?)",
                (
                    json.dumps(
                        {"result_contract": "compact-v1", "audit": {"status": "accepted", "action_scope": scope}}
                    ),
                ),
            )
            c.execute(
                "INSERT INTO github_actions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "push",
                    "r",
                    "t",
                    "push_existing_pr",
                    "",
                    "",
                    "accepted",
                    "not_published",
                    "now",
                    json.dumps(
                        {
                            "worktree_path": self.tmp.name,
                            "remote": "origin",
                            "branch": "codex/fix",
                        }
                    ),
                ),
            )

    def runner(self, command, **kwargs):
        if command[:3] == ["git", "remote", "get-url"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/x/y.git\n", stderr="")
        if command[:3] == ["git", "ls-remote", "--get-url"]:
            return SimpleNamespace(returncode=0, stdout=command[-1] + "\n", stderr="")
        self.calls.append(command[:2])
        if command[:2] == ["git", "ls-remote"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:2] == ["git", "push"]:
            return SimpleNamespace(returncode=1, stdout="", stderr=self.error)
        raise AssertionError(command)

    def cycle(self, seconds=0):
        self.now += timedelta(seconds=seconds)
        with mock.patch.object(publication_values, "now", return_value=self.now.isoformat()):
            return publish.publish_ready_actions(self.db, run_command=self.runner)
