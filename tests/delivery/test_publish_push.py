from contextlib import closing
import json
import sqlite3
from tests.support.publication import PublicationFixture


class PublishPushTests(PublicationFixture):
    def test_publish_ready_push_existing_pr_pushes_branch(self):
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
                    target_url = 'https://github.com/x/y/pull/42',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "worktree_path": str(worktree),
                            "remote": "origin",
                            "branch": "codex/dd-123",
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            returncode = 0
            stdout = "pushed\n"
            stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return Completed()

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
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
        self.assertNotIn("shell", calls[0][1])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                """
                SELECT publish_status, external_id, target_url
                FROM github_actions
                WHERE action_id = 'action-1'
                """
            ).fetchone()
        self.assertEqual(row, ("published", "codex/dd-123", "https://github.com/x/y/pull/42"))

    def test_publish_ready_push_existing_pr_without_audited_head_uses_fast_forward_push(self):
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
                    target_url = 'https://github.com/x/y/pull/42',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "worktree_path": str(worktree),
                            "remote": "origin",
                            "branch": "codex/dd-123",
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return Completed()

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(
            calls[0][0],
            [
                "git",
                "push",
                "origin",
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:refs/heads/codex/dd-123",
            ],
        )

    def test_publish_ready_push_existing_pr_uses_audited_head_sha_for_lease(self):
        from robert_agent.delivery.application import publish
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        worktree = self.root / "worktree"
        worktree.mkdir()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            metadata = json.dumps(
                {
                    "worktree_path": str(worktree),
                    "remote": "origin",
                    "branch": "codex/dd-123",
                },
                sort_keys=True,
            )
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'push_existing_pr',
                    target_url = 'https://github.com/x/y/pull/42',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (metadata,),
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
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:3] == ["git", "ls-remote", "--heads"]:
                completed = Completed()
                completed.stdout = "audited-sha\trefs/heads/codex/dd-123\n"
                return completed
            return Completed()

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(calls[0][0], ["git", "ls-remote", "--heads", "origin", "codex/dd-123"])
        self.assertEqual(
            calls[1][0],
            [
                "git",
                "push",
                "--force-with-lease=codex/dd-123:audited-sha",
                "origin",
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:refs/heads/codex/dd-123",
            ],
        )

    def test_publish_ready_push_existing_pr_skips_diverged_result_actions(self):
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
                    target_url = 'https://github.com/x/y/pull/42',
                    created_at = '2026-10-20T02:00:00+00:00',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "worktree_path": str(worktree),
                            "remote": "origin",
                            "branch": "codex/dd-123",
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
            conn.execute(
                """
                INSERT INTO github_actions(
                  action_id, result_id, task_id, action_type, target_url, external_id,
                  audit_status, publish_status, created_at, metadata_json
                )
                VALUES(
                  'action-2', 'result-1', 'task-1', 'comment',
                  'https://github.com/x/y/pull/42', NULL,
                  'accepted', 'not_published', '2026-10-20T02:00:01+00:00', ?
                )
                """,
                (
                    json.dumps(
                        {
                            "body": self._dd_comment_body("ready"),
                            "target_url": "https://github.com/x/y/pull/42",
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            def __init__(self, returncode=0, stdout=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:3] == ["git", "ls-remote", "--heads"]:
                return Completed(stdout="manual-sha\trefs/heads/codex/dd-123\n")
            if command[:2] == ["git", "fetch"]:
                return Completed()
            if command[:3] == ["git", "merge-base", "--is-ancestor"]:
                return Completed(returncode=1)
            raise AssertionError(command)

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 0)
        self.assertEqual(result["failed_count"], 0)
        self.assertEqual(result["skipped_count"], 2)
        self.assertEqual([call[0] for call in calls], [
            ["git", "ls-remote", "--heads", "origin", "codex/dd-123"],
            [
                "git", "fetch", "--no-tags", "--no-write-fetch-head",
                "origin", "refs/heads/codex/dd-123",
            ],
            ["git", "merge-base", "--is-ancestor", "manual-sha", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
        ])
        with closing(sqlite3.connect(db_path)) as conn:
            actions = conn.execute(
                """
                SELECT action_id, publish_status, metadata_json
                FROM github_actions
                ORDER BY created_at, action_id
                """
            ).fetchall()
        self.assertEqual([row[:2] for row in actions], [
            ("action-1", "skipped"),
            ("action-2", "skipped"),
        ])
        self.assertIn(
            "remote branch diverged from audited result",
            json.loads(actions[0][2])["publish"]["safe_error"],
        )
        self.assertEqual(
            json.loads(actions[1][2])["publish"]["safe_error"],
            "skipped because the preceding push was superseded",
        )

    def test_publish_ready_push_existing_pr_allows_remote_ancestor_of_result(self):
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
                    target_url = 'https://github.com/x/y/pull/42',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "worktree_path": str(worktree),
                            "remote": "origin",
                            "branch": "codex/dd-123",
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
                                    "result_head_sha": "result-sha",
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
            if command[:3] == ["git", "ls-remote", "--heads"]:
                return Completed("current-sha\trefs/heads/codex/dd-123\n")
            if command[:2] == ["git", "fetch"]:
                return Completed()
            if command[:3] == ["git", "merge-base", "--is-ancestor"]:
                return Completed()
            if command[:2] == ["git", "push"]:
                return Completed("pushed\n")
            raise AssertionError(command)

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(
            [call[0] for call in calls],
            [
                ["git", "ls-remote", "--heads", "origin", "codex/dd-123"],
                [
                    "git", "fetch", "--no-tags", "--no-write-fetch-head",
                    "origin", "refs/heads/codex/dd-123",
                ],
                ["git", "merge-base", "--is-ancestor", "current-sha", "result-sha"],
                [
                    "git",
                    "push",
                    "--force-with-lease=codex/dd-123:current-sha",
                    "origin",
                    "result-sha:refs/heads/codex/dd-123",
                ],
            ],
        )

    def test_publish_ready_push_existing_pr_retries_ancestry_check_errors(self):
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
                    target_url = 'https://github.com/x/y/pull/42',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "worktree_path": str(worktree),
                            "remote": "origin",
                            "branch": "codex/dd-123",
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
                                    "result_head_sha": "result-sha",
                                },
                            }
                        },
                        sort_keys=True,
                    ),
                ),
            )

        class Completed:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(command, **_kwargs):
            if command[:3] == ["git", "ls-remote", "--heads"]:
                return Completed(stdout="current-sha\trefs/heads/codex/dd-123\n")
            if command[:2] == ["git", "fetch"]:
                return Completed()
            if command[:3] == ["git", "merge-base", "--is-ancestor"]:
                return Completed(returncode=128, stderr="bad object current-sha")
            raise AssertionError(command)

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["failed_count"], 1)
        self.assertIn("ancestry check failed", result["failures"][0]["safe_error"])
        with closing(sqlite3.connect(db_path)) as conn:
            publish_status = conn.execute(
                "SELECT publish_status FROM github_actions WHERE action_id = 'action-1'"
            ).fetchone()[0]
        self.assertEqual(publish_status, "not_published")

    def test_publish_ready_push_existing_pr_without_audited_head_and_target_url_uses_fast_forward_push(self):
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
                    target_url = 'https://github.com/x/y/pull/42',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "worktree_path": str(worktree),
                            "remote": "origin",
                            "branch": "codex/dd-123",
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            returncode = 0
            stdout = "pushed\n"
            stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return Completed()

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
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
