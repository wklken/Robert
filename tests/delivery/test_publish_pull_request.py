from contextlib import closing
import json
import sqlite3
from tests.support.publication import PublicationFixture


class PublishPullRequestTests(PublicationFixture):
    def test_publish_ready_open_pr_action_creates_pr_and_records_url(self):
        from robert_agent.delivery.application import publish

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'open_pr',
                    target_url = NULL,
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "repo": "x/y",
                            "head": "codex/dd-123",
                            "base": "master",
                            "title": "Fix timeout",
                            "body": self._dd_pr_body("Implements the fix"),
                            "draft": True,
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            def __init__(self, stdout):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:3] == ["gh", "pr", "list"]:
                return Completed("[]")
            return Completed("https://github.com/x/y/pull/42\n")

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(
            calls[0][0],
            [
                "gh",
                "pr",
                "list",
                "--repo",
                "x/y",
                "--head",
                "codex/dd-123",
                "--state",
                "open",
                "--json",
                "number,url,baseRefName,headRefName,headRepositoryOwner",
            ],
        )
        self.assertEqual(
            calls[1][0],
            [
                "gh",
                "pr",
                "create",
                "--repo",
                "x/y",
                "--head",
                "codex/dd-123",
                "--base",
                "master",
                "--title",
                "Fix timeout",
                "--body",
                self._dd_pr_body("Implements the fix"),
                "--draft",
            ],
        )
        self.assertNotIn("shell", calls[1][1])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                """
                SELECT publish_status, external_id, target_url
                FROM github_actions
                WHERE action_id = 'action-1'
                """
            ).fetchone()
        self.assertEqual(row, ("published", "42", "https://github.com/x/y/pull/42"))

    def test_publish_ready_new_pr_pushes_branch_before_opening_pr(self):
        from robert_agent.delivery.application import publish

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        worktree = self.root / "worktree"
        worktree.mkdir()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'push_existing_pr',
                    target_url = NULL,
                    created_at = '2026-06-18T08:00:00+00:00',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "worktree_path": str(worktree),
                            "branch": "codex/dd-123",
                            "remote": "origin",
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.execute(
                """
                INSERT INTO github_actions(
                  action_id, result_id, task_id, action_type, target_url, external_id,
                  audit_status, publish_status, created_at, metadata_json
                )
                VALUES(
                  'action-2', 'result-1', 'task-1', 'open_pr', NULL, NULL,
                  'accepted', 'not_published', '2026-06-18T08:00:01+00:00', ?
                )
                """,
                (
                    json.dumps(
                        {
                            "repo": "x/y",
                            "head": "codex/dd-123",
                            "base": "master",
                            "title": "Fix timeout",
                            "body": self._dd_pr_body("Implements the fix"),
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            def __init__(self, stdout=""):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:2] == ["git", "push"]:
                return Completed("pushed\n")
            if command[:3] == ["gh", "pr", "list"]:
                return Completed("[]")
            if command[:4] == ["git", "remote", "get-url", "origin"]:
                self.assertEqual(kwargs["cwd"], str(worktree))
                return Completed("https://github.com/x/y.git\n")
            if command[:3] == ["gh", "pr", "create"]:
                return Completed("https://github.com/x/y/pull/42\n")
            raise AssertionError(command)

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 2)
        self.assertEqual(
            calls[0][0],
            [
                "git",
                "push",
                "origin",
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:refs/heads/codex/dd-123",
            ],
        )
        self.assertEqual(calls[0][1]["cwd"], str(worktree))
        self.assertEqual(calls[1][0], ["git", "remote", "get-url", "origin"])
        self.assertEqual(calls[2][0][:3], ["gh", "pr", "list"])
        self.assertEqual(calls[3][0][:3], ["gh", "pr", "create"])
        self.assertEqual(calls[3][0][calls[3][0].index("--head") + 1], "codex/dd-123")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            rows = conn.execute(
                """
                SELECT action_id, publish_status, external_id, target_url
                FROM github_actions
                ORDER BY created_at, action_id
                """
            ).fetchall()
        self.assertEqual(
            rows,
            [
                ("action-1", "published", "codex/dd-123", None),
                ("action-2", "published", "42", "https://github.com/x/y/pull/42"),
            ],
        )

    def test_publish_ready_open_pr_uses_fork_owner_from_published_push_action(self):
        from robert_agent.delivery.application import publish

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        worktree = self.root / "worktree"
        worktree.mkdir()
        branch = "codex/dd-2901-bug-mcp-proxy-arraystring-value-bug"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'push_existing_pr',
                    target_url = NULL,
                    external_id = ?,
                    publish_status = 'published',
                    created_at = '2026-06-18T08:00:00+00:00',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    branch,
                    json.dumps(
                        {
                            "worktree_path": str(worktree),
                            "branch": branch,
                            "remote": "origin",
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.execute(
                """
                INSERT INTO github_actions(
                  action_id, result_id, task_id, action_type, target_url, external_id,
                  audit_status, publish_status, created_at, metadata_json
                )
                VALUES(
                  'action-2', 'result-1', 'task-1', 'open_pr', NULL, NULL,
                  'accepted', 'not_published', '2026-06-18T08:00:01+00:00', ?
                )
                """,
                (
                    json.dumps(
                        {
                            "repo": "example/backend",
                            "head": branch,
                            "base": "master",
                            "title": "Fix array string value bug",
                            "body": self._dd_pr_body("Implements the fix"),
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.execute(
                "UPDATE worker_results SET metadata_json=json_set(?, '$.result_contract', 'compact-v1') WHERE result_id = 'result-1'",
                (
                    json.dumps(
                        {
                            "audit": {
                                "status": "accepted",
                                "action_scope": {
                                    "repo": "example/backend",
                                    "sources": [],
                                    "base_branch": "master",
                                    "branch_name": branch,
                                    "worktree_path": str(worktree),
                                    "remote": "origin",
                                },
                            }
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            def __init__(self, stdout=""):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:4] == ["git", "remote", "get-url", "origin"]:
                self.assertEqual(kwargs["cwd"], str(worktree))
                return Completed("https://github.com/robert-bot/blueking-apigateway.git\n")
            if command[:3] == ["gh", "pr", "list"]:
                return Completed("[]")
            if command[:3] == ["gh", "pr", "create"]:
                return Completed("https://github.com/example/backend/pull/42\n")
            raise AssertionError(command)

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(calls[0][0], ["git", "remote", "get-url", "origin"])
        self.assertEqual(calls[1][0][:3], ["gh", "pr", "list"])
        self.assertEqual(calls[1][0][calls[1][0].index("--head") + 1], branch)
        self.assertEqual(calls[2][0][:3], ["gh", "pr", "create"])
        self.assertEqual(calls[2][0][calls[2][0].index("--head") + 1], f"robert-bot:{branch}")

    def test_publish_ready_new_pr_stops_open_pr_when_branch_push_fails(self):
        from robert_agent.delivery.application import publish

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        worktree = self.root / "worktree"
        worktree.mkdir()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'push_existing_pr',
                    target_url = NULL,
                    created_at = '2026-06-18T08:00:00+00:00',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "worktree_path": str(worktree),
                            "branch": "codex/dd-123",
                            "remote": "origin",
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.execute(
                """
                INSERT INTO github_actions(
                  action_id, result_id, task_id, action_type, target_url, external_id,
                  audit_status, publish_status, created_at, metadata_json
                )
                VALUES(
                  'action-2', 'result-1', 'task-1', 'open_pr', NULL, NULL,
                  'accepted', 'not_published', '2026-06-18T08:00:01+00:00', ?
                )
                """,
                (
                    json.dumps(
                        {
                            "repo": "x/y",
                            "head": "codex/dd-123",
                            "base": "master",
                            "title": "Fix timeout",
                            "body": self._dd_pr_body("Implements the fix"),
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:2] == ["git", "push"]:
                return Completed(returncode=1, stderr="push rejected")
            raise AssertionError(command)

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["published_count"], 0)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0][0],
            [
                "git",
                "push",
                "origin",
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:refs/heads/codex/dd-123",
            ],
        )
        with closing(sqlite3.connect(db_path)) as conn, conn:
            rows = conn.execute(
                """
                SELECT action_id, publish_status, external_id, target_url
                FROM github_actions
                ORDER BY created_at, action_id
                """
            ).fetchall()
        self.assertEqual(
            rows,
            [
                ("action-1", "not_published", None, None),
                ("action-2", "not_published", None, None),
            ],
        )

    def test_publish_ready_existing_pr_with_audited_head_stops_when_remote_lookup_fails(
        self,
    ):
        from robert_agent.delivery.application import publish

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        worktree = self.root / "worktree"
        worktree.mkdir()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'push_existing_pr',
                    target_url = NULL,
                    created_at = '2026-06-18T08:00:00+00:00',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "worktree_path": str(worktree),
                            "branch": "codex/dd-123",
                            "remote": "origin",
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.execute(
                """
                UPDATE worker_results
                SET metadata_json=json_set(?, '$.result_contract', 'compact-v1')
                WHERE result_id = 'result-1'
                """,
                (
                    json.dumps(
                        {
                            "audit": {
                                "status": "accepted",
                                "action_scope": {
                                    "worktree_path": str(worktree),
                                    "branch_name": "codex/dd-123",
                                    "remote": "origin",
                                    "existing_pr_head_sha": "audited-sha",
                                    "result_head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                                },
                            }
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:3] == ["git", "ls-remote", "--heads"]:
                return Completed(returncode=128, stderr="network unavailable")
            raise AssertionError(command)

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["published_count"], 0)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(
            [call[0] for call in calls],
            [
                ["git", "ls-remote", "--heads", "origin", "codex/dd-123"],
            ],
        )
        self.assertIn("remote branch lookup failed", result["failures"][0]["safe_error"])

    def test_publish_ready_open_pr_reuses_existing_head_pr(self):
        from robert_agent.delivery.application import publish

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'open_pr',
                    target_url = NULL,
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "repo": "x/y",
                            "head": "codex/dd-123",
                            "base": "master",
                            "title": "Fix timeout",
                            "body": self._dd_pr_body("Implements the fix"),
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            def __init__(self, stdout):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:3] == ["gh", "pr", "list"]:
                return Completed(
                    json.dumps(
                        [
                            {
                                "number": 42,
                                "url": "https://github.com/x/y/pull/42",
                                "baseRefName": "master",
                                "headRefName": "codex/dd-123",
                                "headRepositoryOwner": {"login": "x"},
                            }
                        ]
                    )
                )
            self.assertEqual(command[:4], ["gh", "pr", "view", "42"])
            return Completed(
                json.dumps(
                    {
                        "number": 42,
                        "url": "https://github.com/x/y/pull/42",
                        "baseRefName": "master",
                        "headRefName": "codex/dd-123",
                        "headRepositoryOwner": {"login": "x"},
                        "body": self._dd_pr_body("Existing DD PR"),
                    },
                    sort_keys=True,
                )
            )

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(result["deduplicated_count"], 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][:3], ["gh", "pr", "list"])
        self.assertEqual(
            calls[1][0],
            [
                "gh",
                "pr",
                "view",
                "42",
                "--repo",
                "x/y",
                "--json",
                "number,url,body,baseRefName,headRefName,headRepositoryOwner",
            ],
        )
        with closing(sqlite3.connect(db_path)) as conn, conn:
            action = conn.execute(
                """
                SELECT publish_status, external_id, target_url, metadata_json
                FROM github_actions
                WHERE action_id = 'action-1'
                """
            ).fetchone()
        self.assertEqual(action[:3], ("published", "42", "https://github.com/x/y/pull/42"))
        self.assertTrue(json.loads(action[3])["publish"]["deduplicated"])

    def test_publish_ready_open_pr_ignores_same_head_pr_without_marker(self):
        from robert_agent.delivery.application import publish

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'open_pr',
                    target_url = NULL,
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "repo": "x/y",
                            "head": "codex/dd-123",
                            "base": "master",
                            "title": "Fix timeout",
                            "body": self._dd_pr_body("Implements the fix"),
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            def __init__(self, stdout):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:3] == ["gh", "pr", "list"]:
                return Completed(
                    json.dumps(
                        [
                            {
                                "number": 42,
                                "url": "https://github.com/x/y/pull/42",
                                "baseRefName": "master",
                                "headRefName": "codex/dd-123",
                            }
                        ]
                    )
                )
            if command[:3] == ["gh", "pr", "view"]:
                return Completed(
                    json.dumps(
                        {
                            "number": 42,
                            "url": "https://github.com/x/y/pull/42",
                            "baseRefName": "master",
                            "headRefName": "codex/dd-123",
                            "body": "Manual PR on the same head branch",
                        },
                        sort_keys=True,
                    )
                )
            self.assertEqual(command[:3], ["gh", "pr", "create"])
            return Completed("https://github.com/x/y/pull/43\n")

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(result["deduplicated_count"], 0)
        self.assertEqual(
            [call[0][:3] for call in calls],
            [["gh", "pr", "list"], ["gh", "pr", "view"], ["gh", "pr", "create"]],
        )
        with closing(sqlite3.connect(db_path)) as conn, conn:
            action = conn.execute(
                """
                SELECT publish_status, external_id, target_url, metadata_json
                FROM github_actions
                WHERE action_id = 'action-1'
                """
            ).fetchone()
        self.assertEqual(action[:3], ("published", "43", "https://github.com/x/y/pull/43"))
        self.assertFalse(json.loads(action[3])["publish"]["deduplicated"])

    def test_publish_ready_open_pr_reuses_same_owner_head_pr_with_different_marker(
        self,
    ):
        from robert_agent.delivery.application import publish

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'open_pr',
                    target_url = NULL,
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "repo": "x/y",
                            "head": "codex/dd-123",
                            "head_owner": "robert-bot",
                            "base": "master",
                            "title": "Fix timeout",
                            "body": self._dd_pr_body("Implements the fix"),
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.execute(
                "UPDATE worker_results SET metadata_json=json_set(?, '$.result_contract', 'compact-v1') WHERE result_id = 'result-1'",
                (
                    json.dumps(
                        {
                            "audit": {
                                "status": "accepted",
                                "action_scope": {
                                    "repo": "x/y",
                                    "sources": [{"source_type": "issue", "number": 1}],
                                    "head_owner": "robert-bot",
                                },
                            }
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            def __init__(self, stdout):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:3] == ["gh", "pr", "list"]:
                return Completed(
                    json.dumps(
                        [
                            {
                                "number": 42,
                                "url": "https://github.com/x/y/pull/42",
                                "baseRefName": "master",
                                "headRefName": "codex/dd-123",
                                "headRepositoryOwner": {"login": "robert-bot"},
                            }
                        ]
                    )
                )
            if command[:3] == ["gh", "pr", "view"]:
                return Completed(
                    json.dumps(
                        {
                            "number": 42,
                            "url": "https://github.com/x/y/pull/42",
                            "baseRefName": "master",
                            "headRefName": "codex/dd-123",
                            "headRepositoryOwner": {"login": "robert-bot"},
                            "body": self._dd_pr_body("Existing DD PR", task_id="task-old"),
                        },
                        sort_keys=True,
                    )
                )
            raise AssertionError(command)

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(result["deduplicated_count"], 1)
        self.assertEqual(
            [call[0][:3] for call in calls],
            [["gh", "pr", "list"], ["gh", "pr", "view"]],
        )
        with closing(sqlite3.connect(db_path)) as conn, conn:
            metadata = json.loads(
                conn.execute("SELECT metadata_json FROM github_actions WHERE action_id = 'action-1'").fetchone()[0]
            )
        self.assertEqual(metadata["publish"]["response"]["dedupe_reason"], "matching_head")

    def test_publish_ready_open_pr_does_not_reuse_different_owner_head_pr(self):
        from robert_agent.delivery.application import publish

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'open_pr',
                    target_url = NULL,
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "repo": "x/y",
                            "head": "codex/dd-123",
                            "head_owner": "x",
                            "base": "master",
                            "title": "Fix timeout",
                            "body": self._dd_pr_body("Implements the fix"),
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            def __init__(self, stdout):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:3] == ["gh", "pr", "list"]:
                return Completed(
                    json.dumps(
                        [
                            {
                                "number": 42,
                                "url": "https://github.com/x/y/pull/42",
                                "baseRefName": "master",
                                "headRefName": "codex/dd-123",
                                "headRepositoryOwner": {"login": "someone-else"},
                            }
                        ]
                    )
                )
            if command[:3] == ["gh", "pr", "view"]:
                return Completed(
                    json.dumps(
                        {
                            "number": 42,
                            "url": "https://github.com/x/y/pull/42",
                            "baseRefName": "master",
                            "headRefName": "codex/dd-123",
                            "headRepositoryOwner": {"login": "someone-else"},
                            "body": self._dd_pr_body("Implements the fix"),
                        },
                        sort_keys=True,
                    )
                )
            self.assertEqual(command[:3], ["gh", "pr", "create"])
            return Completed("https://github.com/x/y/pull/43\n")

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(result["deduplicated_count"], 0)
        self.assertEqual(
            [call[0][:3] for call in calls],
            [["gh", "pr", "list"], ["gh", "pr", "view"], ["gh", "pr", "create"]],
        )
        self.assertEqual(calls[2][0][calls[2][0].index("--head") + 1], "codex/dd-123")

    def test_publish_ready_open_pr_empty_stdout_does_not_crash(self):
        from robert_agent.delivery.application import publish

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'open_pr',
                    target_url = NULL,
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "repo": "x/y",
                            "head": "codex/dd-123",
                            "base": "master",
                            "title": "Fix timeout",
                            "body": self._dd_pr_body("Implements the fix"),
                        },
                        sort_keys=True,
                    ),
                ),
            )

        class Completed:
            def __init__(self, stdout):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **_kwargs):
            if command[:3] == ["gh", "pr", "list"]:
                return Completed("[]")
            return Completed("")

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            publish_status = conn.execute(
                "SELECT publish_status FROM github_actions WHERE action_id = 'action-1'"
            ).fetchone()[0]
        self.assertEqual(publish_status, "published")
