from contextlib import closing
import json
import sqlite3
from robert_agent.delivery import results
from tests.support.publication import PublicationFixture


class PublishRemediationTests(PublicationFixture):
    def test_remediation_publish_revalidates_snapshot_and_records_result_head(self):
        from robert_agent.delivery.application import publish

        db_path, worktree = self._init_remediation_publish_db()
        calls = []

        class Completed:
            def __init__(self, stdout=""):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **_kwargs):
            calls.append(command)
            if command == ["gh", "api", "repos/x/y/pulls/42"]:
                return Completed(
                    json.dumps(
                        {
                            "state": "open",
                            "merged": False,
                            "head": {"sha": "head-1", "ref": "codex/fix"},
                            "base": {"sha": "base-1", "ref": "main"},
                        }
                    )
                )
            if command == ["git", "rev-parse", "HEAD"]:
                return Completed("repair-1\n")
            if command[:2] == ["git", "push"]:
                return Completed("pushed\n")
            if command[:3] == [
                "gh",
                "api",
                "repos/x/y/issues/42/comments?per_page=100",
            ]:
                return Completed("[]")
            if command[:3] == ["gh", "api", "repos/x/y/issues/42/comments"]:
                return Completed(
                    json.dumps(
                        {
                            "id": 9001,
                            "html_url": "https://github.com/x/y/pull/42#issuecomment-9001",
                        }
                    )
                )
            raise AssertionError(command)

        result = publish.publish_ready_actions(
            db_path,
            dry_run=False,
            run_command=fake_run,
        )

        self.assertTrue(result["ok"], result)
        self.assertIn(
            ["git", "push", "origin", "repair-1:refs/heads/codex/fix"],
            calls,
        )
        with closing(sqlite3.connect(db_path)) as conn:
            episode = conn.execute(
                """
                SELECT status, result_head_sha
                FROM pr_remediation_episodes
                WHERE episode_id = 'episode-ci'
                """
            ).fetchone()
        self.assertEqual(episode, ("waiting_for_signal", "repair-1"))
        self.assertTrue(worktree.is_dir())

    def test_remediation_retry_recognizes_already_pushed_result(self):
        from robert_agent.delivery.application import publish

        db_path, worktree = self._init_remediation_publish_db()
        calls = []

        class Completed:
            def __init__(self, stdout=""):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **_kwargs):
            calls.append(command)
            if command == ["gh", "api", "repos/x/y/pulls/42"]:
                return Completed(
                    json.dumps(
                        {
                            "state": "open",
                            "merged": False,
                            "head": {"sha": "repair-1", "ref": "codex/fix"},
                            "base": {"sha": "base-1", "ref": "main"},
                        }
                    )
                )
            if command[:3] == [
                "gh",
                "api",
                "repos/x/y/issues/42/comments?per_page=100",
            ]:
                return Completed("[]")
            if command[:3] == ["gh", "api", "repos/x/y/issues/42/comments"]:
                return Completed(
                    json.dumps(
                        {
                            "id": 9001,
                            "html_url": "https://github.com/x/y/pull/42#issuecomment-9001",
                        }
                    )
                )
            raise AssertionError(command)

        result = publish.publish_ready_actions(
            db_path,
            dry_run=False,
            run_command=fake_run,
        )

        self.assertTrue(result["ok"], result)
        self.assertFalse(any(command[0] == "git" for command in calls))
        self.assertEqual(result["deduplicated_count"], 1)
        with closing(sqlite3.connect(db_path)) as conn:
            episode = conn.execute(
                """
                SELECT status, result_head_sha
                FROM pr_remediation_episodes
                WHERE episode_id = 'episode-ci'
                """
            ).fetchone()
        self.assertEqual(episode, ("waiting_for_signal", "repair-1"))
        self.assertTrue(worktree.is_dir())

    def test_remediation_publish_skips_push_and_comment_when_snapshot_changed(self):
        from robert_agent.delivery.application import publish

        db_path, _worktree = self._init_remediation_publish_db()
        calls = []

        class Completed:
            returncode = 0
            stdout = json.dumps(
                {
                    "state": "open",
                    "merged": False,
                    "head": {"sha": "head-new", "ref": "codex/fix"},
                    "base": {"sha": "base-1", "ref": "main"},
                }
            )
            stderr = ""

        def fake_run(command, **_kwargs):
            calls.append(command)
            if command == ["gh", "api", "repos/x/y/pulls/42"]:
                return Completed()
            raise AssertionError(command)

        result = publish.publish_ready_actions(
            db_path,
            dry_run=False,
            limit=1,
            run_command=fake_run,
        )
        second = publish.publish_ready_actions(
            db_path,
            dry_run=False,
            limit=1,
            run_command=fake_run,
        )

        self.assertTrue(result["ok"], result)
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["pending_count"], 0)
        self.assertFalse(any(command[:2] == ["git", "push"] for command in calls))
        self.assertFalse(any(command[:3] == ["gh", "api", "repos/x/y/issues/42/comments"] for command in calls))
        with closing(sqlite3.connect(db_path)) as conn:
            statuses = conn.execute(
                """
                SELECT action_type, publish_status
                FROM github_actions
                WHERE result_id = 'result-1'
                ORDER BY created_at, action_id
                """
            ).fetchall()
            episode_status = conn.execute(
                """
                SELECT status
                FROM pr_remediation_episodes
                WHERE episode_id = 'episode-ci'
                """
            ).fetchone()[0]
            actions_terminal = results.result_actions_all_published(
                conn,
                "result-1",
            )
        self.assertEqual(
            statuses,
            [
                ("push_existing_pr", "skipped"),
                ("comment", "skipped"),
            ],
        )
        self.assertEqual(episode_status, "superseded")
        self.assertTrue(actions_terminal)

    def test_remediation_publish_failure_skips_following_comment(self):
        from robert_agent.delivery.application import publish

        db_path, _worktree = self._init_remediation_publish_db()

        class Completed:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(command, **_kwargs):
            if command == ["gh", "api", "repos/x/y/pulls/42"]:
                return Completed(
                    stdout=json.dumps(
                        {
                            "state": "open",
                            "merged": False,
                            "head": {"sha": "head-1", "ref": "codex/fix"},
                            "base": {"sha": "base-1", "ref": "main"},
                        }
                    )
                )
            if command == ["git", "rev-parse", "HEAD"]:
                return Completed(stdout="repair-1\n")
            if command == [
                "git",
                "push",
                "origin",
                "repair-1:refs/heads/codex/fix",
            ]:
                return Completed(
                    returncode=1,
                    stderr="non-fast-forward",
                )
            raise AssertionError(command)

        result = publish.publish_ready_actions(
            db_path,
            dry_run=False,
            run_command=fake_run,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        with closing(sqlite3.connect(db_path)) as conn:
            statuses = conn.execute(
                """
                SELECT action_type, publish_status
                FROM github_actions
                WHERE result_id = 'result-1'
                ORDER BY action_id
                """
            ).fetchall()
            episode_status = conn.execute(
                """
                SELECT status
                FROM pr_remediation_episodes
                WHERE episode_id = 'episode-ci'
                """
            ).fetchone()[0]
        self.assertEqual(
            statuses,
            [
                ("push_existing_pr", "not_published"),
                ("comment", "skipped"),
            ],
        )
        self.assertEqual(episode_status, "open")

    def test_remediation_publish_rejects_head_changed_after_audit(self):
        from robert_agent.delivery.application import publish

        db_path, _worktree = self._init_remediation_publish_db()
        calls = []

        class Completed:
            def __init__(self, stdout=""):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **_kwargs):
            calls.append(command)
            if command == ["gh", "api", "repos/x/y/pulls/42"]:
                return Completed(
                    json.dumps(
                        {
                            "state": "open",
                            "merged": False,
                            "head": {"sha": "head-1", "ref": "codex/fix"},
                            "base": {"sha": "base-1", "ref": "main"},
                        }
                    )
                )
            if command == ["git", "rev-parse", "HEAD"]:
                return Completed("repair-2\n")
            raise AssertionError(command)

        result = publish.publish_ready_actions(
            db_path,
            dry_run=False,
            run_command=fake_run,
        )

        self.assertFalse(result["ok"])
        self.assertFalse(any(command[:2] == ["git", "push"] for command in calls))
