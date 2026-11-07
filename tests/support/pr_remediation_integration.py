"""Shared fixtures for pr remediation integration tests."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest


class FixtureRunner:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, args, **_kwargs):
        self.calls.append(list(args))
        response = self.responses.get(tuple(args))
        if response is None:
            raise AssertionError(f"unexpected command: {args}")
        stdout = response if isinstance(response, str) else json.dumps(response)
        return subprocess.CompletedProcess(args, 0, stdout, "")


class PrRemediationIntegrationFixture(unittest.TestCase):
    def setUp(self):
        from robert_agent.adapters.sqlite import database as storage

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "robert.sqlite3"
        storage.init_database(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(
                  repo_id, full_name, github_account, default_base_branch,
                  repo_root, worktree_root
                )
                VALUES ('repo:example/backend', 'example/backend', 'robert-bot',
                        'main', '/repo', '/worktrees')
                """
            )
            conn.execute(
                """
                INSERT INTO github_sources(
                  source_id, repo_id, source_key, source_type, number,
                  title, state, author_login
                )
                VALUES
                  ('source:issue', 'repo:example/backend',
                   'github:example/backend#1', 'issue', 1, 'Origin', 'open',
                   'wklken'),
                  ('source:pr', 'repo:example/backend',
                   'github:example/backend!13', 'pull_request', 13, 'Fix',
                   'open', 'robert-bot')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id, lifecycle,
                  created_at, updated_at
                )
                VALUES ('github:example/backend#1', 'repo:example/backend',
                        'source:issue', 'completed', '2026-07-26', '2026-07-26')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id,
                  origin_workstream_id, lifecycle, created_at, updated_at
                )
                VALUES ('github:example/backend!13', 'repo:example/backend',
                        'source:pr', 'github:example/backend#1', 'completed',
                        '2026-07-26', '2026-07-26')
                """
            )
        self.repo = {
            "full_name": "example/backend",
            "github_account": "robert-bot",
            "trusted_actors": ["wklken"],
            "default_base_branch": "main",
            "repo_root": "/repo",
            "worktree_root": "/worktrees",
            "pr_automation": {
                "conflict": {
                    "enabled": True,
                    "max_attempts": 2,
                    "max_wall_minutes": 60,
                },
                "ci": {
                    "enabled": True,
                    "check_allowlist": ["unit"],
                    "max_attempts": 2,
                    "max_wall_minutes": 120,
                    "max_total_tokens": 10000,
                    "max_cost_usd": 10.0,
                    "max_failure_summary_chars": 1000,
                },
            },
        }

    def runner(self, *, author="robert-bot", mergeable=False):
        return FixtureRunner(
            {
                ("gh", "api", "repos/example/backend/pulls/13"): {
                    "state": "open",
                    "merged": False,
                    "mergeable": mergeable,
                    "head": {
                        "sha": "head-1",
                        "ref": "codex/fix",
                        "repo": {"full_name": "example/backend"},
                    },
                    "base": {"sha": "base-1", "ref": "main"},
                    "user": {"login": author},
                    "html_url": "https://github.com/example/backend/pull/13",
                    "updated_at": "2026-07-26T00:00:00Z",
                },
                (
                    "gh",
                    "api",
                    "repos/example/backend/actions/runs?head_sha=head-1&per_page=100",
                ): {
                    "workflow_runs": [
                        {
                            "id": 101,
                            "run_attempt": 1,
                            "name": "unit",
                            "head_sha": "head-1",
                            "status": "completed",
                            "conclusion": "failure",
                            "html_url": "https://github.com/run/101",
                        }
                    ]
                },
                (
                    "gh",
                    "api",
                    "repos/example/backend/actions/runs/101/jobs?per_page=100",
                ): {
                    "jobs": [
                        {
                            "id": 501,
                            "name": "python",
                            "conclusion": "failure",
                        }
                    ]
                },
                (
                    "gh",
                    "api",
                    "repos/example/backend/actions/jobs/501/logs",
                ): "FAIL tests/test_api.py expected 1 got 2",
                (
                    "gh",
                    "api",
                    "repos/example/backend/commits/head-1/check-runs?per_page=100",
                ): {"check_runs": []},
            }
        )

    def setUp_for_non_owned(self):
        from robert_agent.adapters.sqlite import database as storage

        storage.init_database(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(
                  repo_id, full_name, github_account, default_base_branch,
                  repo_root, worktree_root
                )
                VALUES ('repo:example/backend', 'example/backend', 'robert-bot',
                        'main', '/repo', '/worktrees')
                """
            )
            conn.execute(
                """
                INSERT INTO github_sources(
                  source_id, repo_id, source_key, source_type, number,
                  title, state
                )
                VALUES ('source:pr', 'repo:example/backend',
                        'github:example/backend!13', 'pull_request', 13,
                        'Fix', 'open')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id,
                  origin_workstream_id, lifecycle, created_at, updated_at
                )
                VALUES ('origin', 'repo:example/backend', NULL, NULL,
                        'completed', '2026-07-26', '2026-07-26')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id,
                  origin_workstream_id, lifecycle, created_at, updated_at
                )
                VALUES ('github:example/backend!13', 'repo:example/backend',
                        'source:pr', 'origin', 'completed',
                        '2026-07-26', '2026-07-26')
                """
            )
