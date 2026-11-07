from robert_agent.adapters.github import discovery as discover
from contextlib import closing
import json
import sqlite3
import subprocess
from tests.support import PACKAGE_ROOT
from robert_agent.execution import journal as run_journal
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
)


class RepositoryIsolationTests(RepositoryCycleFixture):
    def test_repo_run_steps_can_record_same_step_for_different_repos(self):
        from robert_agent.persistence.schema import database as storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT INTO agent_runs(run_id, status, started_at, config_path, dry_run) VALUES (?, 'running', ?, ?, 1)",
                ("run-test", "2026-07-04T00:00:00+00:00", str(self.config_path)),
            )
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES (?, ?, ?, ?, ?, ?)",
                ("repo:a", "Org/a", "robot", "main", str(self.repo_root), str(self.worktree_root)),
            )
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES (?, ?, ?, ?, ?, ?)",
                ("repo:b", "Org/b", "robot", "main", str(self.repo_root), str(self.worktree_root)),
            )

            run_journal._insert_repo_run_step(conn, "run-test", "repo:a", "discover")
            run_journal._insert_repo_run_step(conn, "run-test", "repo:b", "discover")
            run_journal.mark_repo_run_step(
                conn,
                "run-test",
                "repo:a",
                "discover",
                "succeeded",
                "2026-07-04T00:00:01+00:00",
                output={"raw_event_count": 1},
            )

            rows = conn.execute(
                "SELECT repo_id, step_key, status, output_json FROM run_repo_steps ORDER BY repo_id"
            ).fetchall()

        self.assertEqual(
            [(row[0], row[1], row[2]) for row in rows],
            [
                ("repo:a", "discover", "succeeded"),
                ("repo:b", "discover", "pending"),
            ],
        )
        self.assertIn("raw_event_count", rows[0][3])

    def test_run_once_processes_two_repos_with_repo_specific_trust(self):
        from robert_agent.runtime import cycle as run_once

        repo_b = self.root / "repo-b"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()
        worktree_b = repo_b / ".worktrees"
        worktree_b.mkdir()
        config_path = self.config_path
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + f"""  - full_name: Example/other
    github_account: robot-other
    trusted_actors:
      - other-maintainer
    default_branch: main
    checkout: {repo_b}
    worktrees: {worktree_b}
""",
            encoding="utf-8",
        )

        def fake_collect(repo, known_workstreams=None, notification_hints=None, **_kwargs):
            if repo["full_name"] == "example/backend":
                return [
                    {
                        "id": "comment-a",
                        "number": 1,
                        "source_type": "issue",
                        "event_type": "comment",
                        "actor_login": "wklken",
                        "author_association": "MEMBER",
                        "body": f"@{repo['github_account']} please analyze",
                    }
                ]
            if repo["full_name"] == "Example/other":
                return [
                    {
                        "id": "comment-b",
                        "number": 2,
                        "source_type": "issue",
                        "event_type": "comment",
                        "actor_login": "other-maintainer",
                        "author_association": "MEMBER",
                        "body": f"@{repo['github_account']} please analyze",
                    }
                ]
            raise AssertionError(repo)

        original_collect = discover.collect_live_events
        try:
            discover.collect_live_events = fake_collect
            result = run_once.run_once(
                config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=False,
            )
        finally:
            discover.collect_live_events = original_collect

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        with closing(sqlite3.connect(result["db_path"])) as conn:
            repos = conn.execute("SELECT full_name FROM repos ORDER BY full_name").fetchall()
            tasks = conn.execute(
                """
                SELECT r.full_name, COUNT(*)
                FROM tasks t
                JOIN workstreams w ON w.workstream_id = t.workstream_id
                JOIN repos r ON r.repo_id = w.repo_id
                GROUP BY r.full_name
                ORDER BY r.full_name
                """
            ).fetchall()
            repo_steps = conn.execute("SELECT COUNT(*) FROM run_repo_steps WHERE step_key = 'route'").fetchone()[0]
            run_summary = json.loads(
                conn.execute("SELECT summary_json FROM agent_runs ORDER BY started_at DESC LIMIT 1").fetchone()[0]
            )

        self.assertEqual(
            [row[0] for row in repos],
            ["Example/other", "example/backend"],
        )
        self.assertEqual(tasks, [("Example/other", 1), ("example/backend", 1)])
        self.assertEqual(repo_steps, 2)
        self.assertEqual(len(run_summary["repo_summaries"]), 2)

    def test_run_once_continues_after_one_repo_discovery_failure(self):
        from robert_agent.runtime import cycle as run_once

        repo_b = self.root / "repo-b"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()
        worktree_b = repo_b / ".worktrees"
        worktree_b.mkdir()
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8")
            + f"""  - full_name: Example/other
    github_account: robot-other
    trusted_actors:
      - other-maintainer
    default_branch: main
    checkout: {repo_b}
    worktrees: {worktree_b}
""",
            encoding="utf-8",
        )

        def fake_collect(repo, **_kwargs):
            if repo["full_name"] == "example/backend":
                raise subprocess.CalledProcessError(1, ["gh", "search", "issues"])
            return [
                {
                    "id": "comment-b",
                    "number": 2,
                    "source_type": "issue",
                    "event_type": "comment",
                    "actor_login": "other-maintainer",
                    "author_association": "MEMBER",
                    "body": f"@{repo['github_account']} please analyze",
                }
            ]

        original_collect = discover.collect_live_events
        try:
            discover.collect_live_events = fake_collect
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=False,
            )
        finally:
            discover.collect_live_events = original_collect

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(len(result["repo_summaries"]), 2)
        with closing(sqlite3.connect(result["db_path"])) as conn:
            run = conn.execute(
                "SELECT status, summary_json FROM agent_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            active_leases = conn.execute(
                "SELECT COUNT(*) FROM leases WHERE resource_type = 'agent_run' AND status = 'active'"
            ).fetchone()[0]

        self.assertEqual(run[0], "failed")
        self.assertEqual(json.loads(run[1])["overall_status"], "partial_failure")
        self.assertEqual(active_leases, 0)

    def test_run_once_multi_repo_top_level_notifications_disable_repo_notification_polling(self):
        from robert_agent.runtime import cycle as run_once

        repo_b = self.root / "repo-b"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()
        worktree_b = repo_b / ".worktrees"
        worktree_b.mkdir()
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8")
            + f"""  - full_name: Example/other
    github_account: robot-other
    trusted_actors:
      - other-maintainer
    default_branch: main
    checkout: {repo_b}
    worktrees: {worktree_b}
""",
            encoding="utf-8",
        )

        discovery_calls = []

        def fake_collect_account_notifications(repos, runner=None):
            self.assertEqual(
                [repo["full_name"] for repo in repos],
                ["example/backend", "Example/other"],
            )
            self.assertIsNotNone(runner)
            return {
                "example/backend": [
                    {
                        "id": "notification-a",
                        "reason": "mention",
                        "repository": {"full_name": "example/backend"},
                    }
                ]
            }

        def fake_collect_live_events(
            repo,
            known_workstreams=None,
            notification_hints=None,
            include_notifications=True,
            **_kwargs,
        ):
            discovery_calls.append(
                {
                    "repo": repo["full_name"],
                    "known_workstreams": known_workstreams,
                    "notification_hints": notification_hints,
                    "include_notifications": include_notifications,
                }
            )
            actor_login = "wklken"
            if repo["full_name"] == "Example/other":
                actor_login = "other-maintainer"
            return [
                {
                    "id": f"comment-{repo['full_name']}",
                    "number": 1 if repo["full_name"] == "example/backend" else 2,
                    "source_type": "issue",
                    "event_type": "comment",
                    "actor_login": actor_login,
                    "author_association": "MEMBER",
                    "body": f"@{repo['github_account']} please analyze",
                }
            ]

        original_collect_notifications = discover.collect_account_notifications
        original_collect_live_events = discover.collect_live_events
        try:
            discover.collect_account_notifications = fake_collect_account_notifications
            discover.collect_live_events = fake_collect_live_events
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=False,
            )
        finally:
            discover.collect_account_notifications = original_collect_notifications
            discover.collect_live_events = original_collect_live_events

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            discovery_calls,
            [
                {
                    "repo": "example/backend",
                    "known_workstreams": set(),
                    "notification_hints": [
                        {
                            "id": "notification-a",
                            "reason": "mention",
                            "repository": {"full_name": "example/backend"},
                        }
                    ],
                    "include_notifications": False,
                },
                {
                    "repo": "Example/other",
                    "known_workstreams": set(),
                    "notification_hints": [],
                    "include_notifications": False,
                },
            ],
        )

    def test_run_once_single_repo_empty_top_level_notifications_disable_repo_polling(self):
        from robert_agent.runtime import cycle as run_once

        discovery_calls = []

        def fake_collect_account_notifications(repos, runner=None):
            self.assertEqual(
                [repo["full_name"] for repo in repos],
                ["example/backend"],
            )
            self.assertIsNotNone(runner)
            return {}

        def fake_collect_live_events(
            repo,
            known_workstreams=None,
            notification_hints=None,
            include_notifications=True,
            **_kwargs,
        ):
            discovery_calls.append(
                {
                    "repo": repo["full_name"],
                    "notification_hints": notification_hints,
                    "include_notifications": include_notifications,
                }
            )
            return []

        original_collect_notifications = discover.collect_account_notifications
        original_collect_live_events = discover.collect_live_events
        try:
            discover.collect_account_notifications = fake_collect_account_notifications
            discover.collect_live_events = fake_collect_live_events
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=False,
            )
        finally:
            discover.collect_account_notifications = original_collect_notifications
            discover.collect_live_events = original_collect_live_events

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            discovery_calls,
            [
                {
                    "repo": "example/backend",
                    "notification_hints": [],
                    "include_notifications": False,
                },
            ],
        )

    def test_run_once_multi_repo_notification_collection_failure_falls_back_per_repo(self):
        from robert_agent.runtime import cycle as run_once

        repo_b = self.root / "repo-b"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()
        worktree_b = repo_b / ".worktrees"
        worktree_b.mkdir()
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8")
            + f"""  - full_name: Example/other
    github_account: robot-other
    trusted_actors:
      - other-maintainer
    default_branch: main
    checkout: {repo_b}
    worktrees: {worktree_b}
""",
            encoding="utf-8",
        )

        discovery_calls = []

        def fake_collect_account_notifications(_repos, runner=None):
            raise subprocess.CalledProcessError(1, ["gh", "api", "notifications"])

        def fake_collect_live_events(
            repo,
            known_workstreams=None,
            notification_hints=None,
            include_notifications=True,
            **_kwargs,
        ):
            discovery_calls.append(
                {
                    "repo": repo["full_name"],
                    "notification_hints": notification_hints,
                    "include_notifications": include_notifications,
                }
            )
            actor_login = "wklken"
            if repo["full_name"] == "Example/other":
                actor_login = "other-maintainer"
            return [
                {
                    "id": f"comment-{repo['full_name']}",
                    "number": 1 if repo["full_name"] == "example/backend" else 2,
                    "source_type": "issue",
                    "event_type": "comment",
                    "actor_login": actor_login,
                    "author_association": "MEMBER",
                    "body": f"@{repo['github_account']} please analyze",
                }
            ]

        original_collect_notifications = discover.collect_account_notifications
        original_collect_live_events = discover.collect_live_events
        try:
            discover.collect_account_notifications = fake_collect_account_notifications
            discover.collect_live_events = fake_collect_live_events
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=False,
            )
        finally:
            discover.collect_account_notifications = original_collect_notifications
            discover.collect_live_events = original_collect_live_events

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            discovery_calls,
            [
                {
                    "repo": "example/backend",
                    "notification_hints": None,
                    "include_notifications": True,
                },
                {
                    "repo": "Example/other",
                    "notification_hints": None,
                    "include_notifications": True,
                },
            ],
        )

    def test_run_once_notification_command_missing_falls_back_per_repo(self):
        from robert_agent.runtime import cycle as run_once

        discovery_calls = []

        def fake_collect_account_notifications(_repos, runner=None):
            raise FileNotFoundError("gh")

        def fake_collect_live_events(
            repo,
            known_workstreams=None,
            notification_hints=None,
            include_notifications=True,
            **_kwargs,
        ):
            discovery_calls.append(
                {
                    "repo": repo["full_name"],
                    "notification_hints": notification_hints,
                    "include_notifications": include_notifications,
                }
            )
            return []

        original_collect_notifications = discover.collect_account_notifications
        original_collect_live_events = discover.collect_live_events
        try:
            discover.collect_account_notifications = fake_collect_account_notifications
            discover.collect_live_events = fake_collect_live_events
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=False,
            )
        finally:
            discover.collect_account_notifications = original_collect_notifications
            discover.collect_live_events = original_collect_live_events

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            discovery_calls,
            [
                {
                    "repo": "example/backend",
                    "notification_hints": None,
                    "include_notifications": True,
                },
            ],
        )
